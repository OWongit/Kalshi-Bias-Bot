"""
Kalshi Automated Trading Bot — main loop.

Discovers markets, opens positions under strict entry rules, sizes bets
from your balance, and manages risk with a stop-loss.
"""

import logging
import sys
import time
from datetime import datetime

try:
    import colorama
    colorama.init()
except ImportError:
    colorama = None

import config
from api_client import KalshiClient
from discovery import discover_all
from trading import (
    build_candidates,
    compute_order_sizes,
    fetch_prices_batch,
    place_entry_orders,
    run_stop_loss,
)

# Quiet mode: only show placed/sold orders, errors, and balance when orders change
logging.basicConfig(
    level=logging.WARNING,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("bot")

# ANSI colors (Raspberry Pi terminal supports these; colorama enables on Windows)
def _c(code: str) -> str:
    return f"\033[{code}m"

C = {
    "reset": _c("0"),
    "bold": _c("1"),
    "green": _c("32"),
    "yellow": _c("33"),
    "red": _c("31"),
    "cyan": _c("36"),
    "magenta": _c("35"),
    "gold": _c("38;5;220"),
    "neon_green": _c("92"),
    "dim": _c("2"),
}


def _timestamp() -> str:
    return f"{C['gold']}{datetime.now().strftime('%-I:%M %p')}{C['reset']}"


def _print_order_placed(ticker: str, count: int, price: int, order_id: str, side: str, dry_run: bool) -> None:
    prefix = f"{C['yellow']}[DRY RUN] " if dry_run else ""
    print(f"{_timestamp()} {prefix}{C['green']}{C['bold']}BUY {side.upper()}{C['reset']} {ticker} × {count} @ {price}¢  order_id={order_id}")


def _print_order_sold(ticker: str, side: str, qty: int, price: int, order_id: str, dry_run: bool) -> None:
    prefix = f"{C['yellow']}[DRY RUN] " if dry_run else ""
    print(f"{_timestamp()} {prefix}{C['red']}{C['bold']}SOLD {side}{C['reset']} {ticker} × {qty} @ {price}¢  order_id={order_id}")


def _print_startup_banner(balance: int, positions: list) -> None:
    cfg = config.load_categories_config(config.CATEGORIES_FILE)
    categories = cfg["categories"]
    defaults = cfg["defaults"]
    open_pos = sum(1 for p in positions if p.get("position", 0) != 0)

    bar = f"{C['neon_green']}{'═' * 60}{C['reset']}"
    print()
    print(bar)
    print(f"{C['neon_green']}{C['bold']}  KALSHI BIAS BOT{C['reset']}")
    print(bar)
    print(f"  {C['cyan']}Balance:{C['reset']}  {C['bold']}${balance / 100:.2f}{C['reset']}  ({balance}¢)")
    print(f"  {C['cyan']}Open positions:{C['reset']}  {C['bold']}{open_pos}{C['reset']}")
    print(f"  {C['cyan']}Dry run:{C['reset']}  {C['yellow']}{config.DRY_RUN}{C['reset']}")
    print()
    print(f"  {C['magenta']}{C['bold']}Active Markets{C['reset']}")
    print(f"  {C['dim']}{'─' * 56}{C['reset']}")
    for cat in categories:
        slug = cat.get("slug", "?")
        side = cat.get("side", defaults.get("side", "no")).upper()
        entry = cat.get("entry_price", defaults.get("entry_price", []))
        spread = cat.get("max_spread", defaults.get("max_spread", 0))
        oi = cat.get("min_open_interest", defaults.get("min_open_interest"))
        oi_str = f"{oi:,}" if oi is not None else "off"
        ep_str = ", ".join(str(e) for e in entry) if isinstance(entry, list) else str(entry)
        side_color = C['green'] if side == "NO" else (C['yellow'] if side == "YES" else C['cyan'])
        print(
            f"  {C['gold']}{slug:14s}{C['reset']}  "
            f"side={side_color}{C['bold']}{side:4s}{C['reset']}  "
            f"entry=[{C['neon_green']}{ep_str}{C['reset']}]¢  "
            f"spread≤{spread}¢  OI≥{oi_str}"
        )
    print(f"  {C['dim']}{'─' * 56}{C['reset']}")
    print(bar)
    print()


def _print_balance_positions(balance: int, positions: list) -> None:
    open_count = sum(1 for p in positions if p.get("position", 0) != 0)
    print(f"  {C['cyan']}Balance: {balance}¢ (${balance/100:.2f})  |  Open positions: {open_count}{C['reset']}")


def main() -> None:
    # -- startup -----------------------------------------------------------
    private_key = config.load_private_key()
    client = KalshiClient(config.BASE_URL, config.API_KEY_ID, private_key)

    try:
        balance = client.get_balance()
    except Exception as exc:
        print(f"{C['red']}{C['bold']}Authentication failed: {exc}{C['reset']}")
        print(f"{C['red']}Check API_KEY_ID, private key, and BASE_URL (demo vs production).{C['reset']}")
        sys.exit(1)

    positions = client.get_positions()
    _print_startup_banner(balance, positions)

    # ticker -> timestamp when cooldown expires
    cooldown_map: dict[str, float] = {}
    # tickers we just placed orders for (avoids duplicates when get_orders lags)
    recently_placed_tickers: set[str] = set()

    # -- main loop ---------------------------------------------------------
    while True:
        try:
            _run_iteration(client, cooldown_map, recently_placed_tickers)
        except KeyboardInterrupt:
            break
        except Exception:
            print(f"{C['red']}{C['bold']}Error:{C['reset']}")
            log.exception("Unhandled error in main loop; will retry after sleep")

        try:
            time.sleep(config.STOP_LOSS_POLL_SECONDS)
        except KeyboardInterrupt:
            break


def _extract_series(ticker: str) -> str:
    """Extract series ticker from market ticker (e.g. KXETH15M-26MAR191930-30 -> KXETH15M)."""
    return ticker.split("-", 1)[0] if "-" in ticker else ticker


def _run_iteration(
    client: KalshiClient,
    cooldown_map: dict[str, float],
    recently_placed_tickers: set[str],
) -> None:
    now = time.time()

    # 1. Discovery
    candidates, series_to_params = discover_all(
        client,
        config.CATEGORIES_FILE,
    )

    cfg = config.load_categories_config(config.CATEGORIES_FILE)
    defaults = cfg["defaults"]

    # Build ticker -> params for candidates and positions
    ticker_to_params: dict[str, dict] = {t: params for t, _, params in candidates}

    # 2. Portfolio state
    positions = client.get_positions()
    balance = client.get_balance()
    try:
        open_orders = client.get_orders(status="resting")
    except Exception:
        log.warning("Failed to fetch open orders; using recently_placed only")
        open_orders = []
    tickers_with_open_orders: set[str] = {o.get("ticker", "") for o in open_orders if o.get("ticker")}

    current_no_tickers: set[str] = set()
    current_yes_tickers: set[str] = set()
    for pos in positions:
        p = pos.get("position", 0)
        if p < 0:
            current_no_tickers.add(pos["ticker"])
        elif p > 0:
            current_yes_tickers.add(pos["ticker"])
        # Ensure position tickers have params (for stop-loss and cooldown)
        t = pos.get("ticker", "")
        if t and t not in ticker_to_params:
            series = _extract_series(t)
            ticker_to_params[t] = series_to_params.get(series, defaults)

    # Once the API reports a non-zero position, drop from recently_placed — position
    # exclusion takes over. Do NOT remove recently_placed when only a resting order
    # exists: if the order fills before get_positions updates, clearing recently_placed
    # on "seen in open_orders" leaves a window where neither resting nor position
    # blocks a second same-market order (duplicate ~2x size).
    recently_placed_tickers -= current_no_tickers | current_yes_tickers

    # Exclude open orders and tickers we just placed (handles get_orders / position lag)
    tickers_with_open_orders = tickers_with_open_orders | recently_placed_tickers

    # 3. Fetch prices for all candidate + position tickers
    all_tickers = list(
        {t for t, _, _ in candidates}
        | {pos["ticker"] for pos in positions if pos.get("position", 0) != 0}
    )
    prices = fetch_prices_batch(client, all_tickers) if all_tickers else {}

    # 4. Prune expired cooldowns
    expired = [t for t, exp in cooldown_map.items() if now >= exp]
    for t in expired:
        del cooldown_map[t]

    active_cooldowns = set(cooldown_map.keys())

    # 5. Build candidate list (YES, NO, or both per category config)
    qualified = build_candidates(
        prices, candidates, active_cooldowns,
    )

    # 6. Compute order sizes (exclude markets with positions or open orders)
    orders_to_place = compute_order_sizes(
        balance, qualified, current_no_tickers, current_yes_tickers, tickers_with_open_orders,
        config.MAX_PCT_PER_MARKET, config.MAX_OPEN_POSITIONS,
        config.MIN_CONTRACTS, config.MAX_CONTRACTS,
    )

    # 7. Place entry orders
    placed = place_entry_orders(
        client, orders_to_place, prices,
        config.DRY_RUN,
    )
    for ticker, count, price, order_id, side in placed:
        _print_order_placed(ticker, count, price, order_id, side, config.DRY_RUN)
    if placed:
        recently_placed_tickers.update(ticker for ticker, _, _, _, _ in placed)
        balance = client.get_balance()
        positions = client.get_positions()
        _print_balance_positions(balance, positions)

    # 8. Stop-loss: build per-ticker stop_loss map
    stop_loss_map: dict[str, int] = {}
    for pos in positions:
        t = pos.get("ticker", "")
        if t and pos.get("position", 0) != 0:
            params = ticker_to_params.get(t, defaults)
            stop_loss_map[t] = params.get("stop_loss", defaults.get("stop_loss", 70))

    sold_tickers, sold_orders = run_stop_loss(
        client, positions, prices,
        stop_loss_map, config.DRY_RUN,
    )
    for ticker, side, qty, price, order_id in sold_orders:
        _print_order_sold(ticker, side, qty, price, order_id, config.DRY_RUN)
    if sold_orders:
        balance = client.get_balance()
        positions = client.get_positions()
        _print_balance_positions(balance, positions)

    for t in sold_tickers:
        params = ticker_to_params.get(t, defaults)
        cooldown_secs = params.get("stop_out_cooldown_seconds", defaults.get("stop_out_cooldown_seconds", 300))
        cooldown_map[t] = now + cooldown_secs


if __name__ == "__main__":
    main()
