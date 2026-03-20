"""
Kalshi Automated NO Trading Bot — main loop.

Discovers markets, opens NO positions under strict entry rules, sizes bets
from your balance, and manages risk with a stop-loss.
"""

import logging
import sys
import time

try:
    import colorama
    colorama.init()
except ImportError:
    colorama = None

import config
from api_client import KalshiClient
from discovery import discover_all
from trading import (
    build_no_candidates,
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
}


def _print_order_placed(ticker: str, count: int, price: int, order_id: str, dry_run: bool) -> None:
    prefix = f"{C['yellow']}[DRY RUN] " if dry_run else ""
    print(f"{prefix}{C['green']}{C['bold']}BUY NO{C['reset']} {ticker} × {count} @ {price}¢  order_id={order_id}")


def _print_order_sold(ticker: str, side: str, qty: int, price: int, order_id: str, dry_run: bool) -> None:
    prefix = f"{C['yellow']}[DRY RUN] " if dry_run else ""
    print(f"{prefix}{C['red']}{C['bold']}SELL {side}{C['reset']} {ticker} × {qty} @ {price}¢  order_id={order_id}")


def _print_balance_positions(balance: int, positions: list) -> None:
    open_count = sum(1 for p in positions if p.get("position", 0) != 0)
    print(f"  {C['cyan']}Balance: {balance}¢ (${balance/100:.2f})  |  Open positions: {open_count}{C['reset']}")


def main() -> None:
    # -- startup -----------------------------------------------------------
    private_key = config.load_private_key()
    client = KalshiClient(config.BASE_URL, config.API_KEY_ID, private_key)

    try:
        client.get_balance()
    except Exception as exc:
        print(f"{C['red']}{C['bold']}Authentication failed: {exc}{C['reset']}")
        print(f"{C['red']}Check API_KEY_ID, private key, and BASE_URL (demo vs production).{C['reset']}")
        sys.exit(1)

    # ticker -> timestamp when cooldown expires
    cooldown_map: dict[str, float] = {}

    # -- main loop ---------------------------------------------------------
    while True:
        try:
            _run_iteration(client, cooldown_map)
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


def _run_iteration(client: KalshiClient, cooldown_map: dict[str, float]) -> None:
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

    # 5. Build NO candidate list
    qualified = build_no_candidates(
        prices, candidates, active_cooldowns,
    )

    # 6. Compute order sizes
    orders_to_place = compute_order_sizes(
        balance, qualified, current_no_tickers,
        config.MAX_PCT_PER_MARKET, config.MAX_OPEN_POSITIONS,
        config.MIN_CONTRACTS, config.MAX_CONTRACTS,
    )

    # 7. Place entry orders
    placed = place_entry_orders(
        client, orders_to_place, prices,
        config.DRY_RUN,
    )
    for ticker, count, price, order_id in placed:
        _print_order_placed(ticker, count, price, order_id, config.DRY_RUN)
    if placed:
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
