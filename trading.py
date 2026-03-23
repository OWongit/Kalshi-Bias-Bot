"""
Trading logic: price fetching, entry filtering, bet sizing, order placement,
and stop-loss management.
"""

import logging
import time

from api_client import parse_market_prices

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Price fetching
# ---------------------------------------------------------------------------

def fetch_prices_batch(
    client,
    tickers: list[str],
    batch_size: int = 20,
) -> dict[str, dict]:
    """Fetch bid/ask data for *tickers* in batches, returning
    {ticker: {yes_bid, yes_ask, no_bid, no_ask}} in cents."""
    prices: dict[str, dict] = {}
    for i in range(0, len(tickers), batch_size):
        batch = tickers[i : i + batch_size]
        markets, _ = client.get_markets(tickers=",".join(batch))
        for m in markets:
            prices[m["ticker"]] = parse_market_prices(m)
    return prices


# ---------------------------------------------------------------------------
# Entry filtering
# ---------------------------------------------------------------------------

def _check_side_entry(
    p: dict, entry_prices: list[int], max_spread: int,
    min_open_interest: int | None, side: str,
) -> int | None:
    """Check if entry criteria are met for *side* on this market.

    Returns the bid in cents if entry qualifies, else None.
    """
    if side == "no":
        ask = p.get("no_ask")
        bid = p.get("no_bid")
    else:
        ask = p.get("yes_ask")
        bid = p.get("yes_bid")
    if ask is None or bid is None:
        return None
    if ask not in entry_prices:
        return None
    if ask - bid > max_spread:
        return None
    if bid < 1 or bid >= 100:
        return None
    oi = p.get("open_interest", 0) or 0
    if min_open_interest is not None and oi < min_open_interest:
        return None
    return bid


def build_candidates(
    prices: dict[str, dict],
    candidates: list[tuple[str, str, dict]],
    cooldown_tickers: set[str],
) -> list[tuple[str, str, int, str, dict]]:
    """Filter candidates to those meeting entry rules for their configured side.

    candidates: list of (ticker, event_ticker, params) with per-category params.
    params["side"] controls which side(s) to check: "yes", "no", or "both".
    Returns [(ticker, event_ticker, bid, side, params)] for qualified markets.
    """
    qualified: list[tuple[str, str, int, str, dict]] = []
    for ticker, event_ticker, params in candidates:
        if ticker in cooldown_tickers:
            log.debug("Skip %s — in cooldown", ticker)
            continue
        p = prices.get(ticker)
        if p is None:
            continue
        entry_prices = params.get("entry_price", [])
        max_spread = params.get("max_spread", 99)
        min_open_interest = params.get("min_open_interest")
        cfg_side = params.get("side", "no")

        if cfg_side == "both":
            sides_to_try = ["no", "yes"]
        else:
            sides_to_try = [cfg_side]

        for s in sides_to_try:
            bid = _check_side_entry(p, entry_prices, max_spread, min_open_interest, s)
            if bid is not None:
                qualified.append((ticker, event_ticker, bid, s, params))
                break

    log.info("%d candidates qualify for entry", len(qualified))
    return qualified


# ---------------------------------------------------------------------------
# Bet sizing
# ---------------------------------------------------------------------------

def compute_order_sizes(
    balance_cents: int,
    qualified: list[tuple[str, str, int, str, dict]],
    current_no_tickers: set[str],
    current_yes_tickers: set[str],
    tickers_with_open_orders: set[str],
    max_pct: float,
    max_positions: int,
    min_contracts: int,
    max_contracts: int,
) -> list[tuple[str, str, int, str, dict]]:
    """Decide how many contracts to buy for each qualified market.

    Skips markets that already have a position or an open order.
    Returns [(ticker, event_ticker, count, side, params)].
    """
    excluded = current_no_tickers | current_yes_tickers | tickers_with_open_orders
    available_slots = max_positions - len(current_no_tickers | current_yes_tickers)
    if available_slots <= 0:
        log.info("No open position slots (max %d reached)", max_positions)
        return []

    orders: list[tuple[str, str, int, str, dict]] = []
    for ticker, event_ticker, bid, side, params in qualified:
        if len(orders) >= available_slots:
            break
        if ticker in excluded:
            continue
        allocation = balance_cents * max_pct
        if bid <= 0:
            continue
        count = int(allocation // bid)
        count = max(min_contracts, min(max_contracts, count))
        orders.append((ticker, event_ticker, count, side, params))
    log.info("Sized %d entry orders", len(orders))
    return orders


# ---------------------------------------------------------------------------
# Order placement
# ---------------------------------------------------------------------------

def _supports_deci_cent_at_price(structure: str, no_bid_cents: int) -> bool:
    """True if market supports 0.1¢ tick at this price level."""
    if structure == "deci_cent":
        return True
    if structure == "tapered_deci_cent":
        return no_bid_cents < 10 or no_bid_cents > 90
    return False


def _format_limit_price(
    no_bid: int,
    no_bid_dollars: str | None,
    structure: str,
    offset_deci_cents: bool,
) -> tuple[int | None, str | None, int | float]:
    """Compute limit price (bid + 0.1¢ or +1¢). Returns (price_cents, price_dollars, display_price)."""
    if offset_deci_cents and _supports_deci_cent_at_price(structure, no_bid):
        # Use no_price_dollars: bid + 0.001 dollars (0.1¢)
        if no_bid_dollars:
            try:
                bid_val = float(no_bid_dollars)
                limit_val = min(0.99, bid_val + 0.001)
                price_dollars = f"{limit_val:.4f}"
                display_price = round(limit_val * 1000) / 10  # e.g. 94.1
                return (round(limit_val * 100), price_dollars, display_price)
            except (ValueError, TypeError):
                pass
        # Fallback: compute from cents
        limit_cents = min(99, no_bid + 1)  # 1¢ when dollars unavailable
        return (limit_cents, None, limit_cents)
    # linear_cent or middle of tapered: +1¢
    limit_cents = max(1, min(99, no_bid + 1))
    return (limit_cents, None, limit_cents)


def place_entry_orders(
    client,
    orders_to_place: list[tuple[str, str, int, str, dict]],
    prices: dict[str, dict],
    dry_run: bool,
) -> list[tuple[str, int, int | float, str, str]]:
    """Place limit-buy orders for each entry, with a final re-check.
    Uses price_dollars for deci-cent markets (0.1¢ above bid), else price_cents (+1¢).
    Returns [(ticker, count, price_cents_or_float, order_id, side), ...] for placed orders."""
    placed: list[tuple[str, int, int | float, str, str]] = []
    for ticker, event_ticker, count, side, params in orders_to_place:
        p = prices.get(ticker)
        if p is None:
            log.warning("No price data for %s on re-check; skipping", ticker)
            continue

        if side == "no":
            ask = p.get("no_ask")
            bid = p.get("no_bid")
            bid_dollars = p.get("no_bid_dollars")
        else:
            ask = p.get("yes_ask")
            bid = p.get("yes_bid")
            bid_dollars = p.get("yes_bid_dollars")

        if ask is None or bid is None:
            log.warning("Missing bid/ask for %s on re-check; skipping", ticker)
            continue
        entry_prices = params.get("entry_price", [])
        max_spread = params.get("max_spread", 99)
        if ask not in entry_prices:
            log.info("%s ask for %s moved to %s; skipping", side.upper(), ticker, ask)
            continue
        if ask - bid > max_spread:
            log.info("Spread widened for %s; skipping", ticker)
            continue
        if bid < 1 or bid >= 100:
            log.info("%s bid %s out of range for %s; skipping", side.upper(), bid, ticker)
            continue

        structure = p.get("price_level_structure", "linear_cent")
        price_cents, price_dollars, display_price = _format_limit_price(
            bid, bid_dollars, structure, offset_deci_cents=True
        )

        if dry_run:
            placed.append((ticker, count, display_price, "dry-run", side))
            continue

        try:
            if price_dollars is not None:
                order = client.create_order(
                    ticker=ticker,
                    side=side,
                    action="buy",
                    count=count,
                    price_dollars=price_dollars,
                )
            else:
                order = client.create_order(
                    ticker=ticker,
                    side=side,
                    action="buy",
                    count=count,
                    price_cents=price_cents,
                )
            placed.append((ticker, count, display_price, order.get("order_id", "?"), side))
        except Exception:
            log.exception("Failed to place order for %s", ticker)
        time.sleep(0.1)

    return placed


# ---------------------------------------------------------------------------
# Stop-loss
# ---------------------------------------------------------------------------

def run_stop_loss(
    client,
    positions: list[dict],
    prices: dict[str, dict],
    stop_loss_map: dict[str, int],
    dry_run: bool,
) -> tuple[set[str], list[tuple[str, str, int, int | float, str]]]:
    """Sell positions whose bid has dropped to or below the stop-loss threshold.

    stop_loss_map: ticker -> stop_loss_cents (per-market).
    Returns (sold_tickers, [(ticker, side, qty, price_cents_or_float, order_id), ...])."""
    sold: set[str] = set()
    sold_orders: list[tuple[str, str, int, int | float, str]] = []
    for pos in positions:
        ticker = pos.get("ticker", "")
        position = pos.get("position", 0)
        if position == 0:
            continue
        stop_loss_cents = stop_loss_map.get(ticker)
        if stop_loss_cents is None or stop_loss_cents == 0:
            continue  # 0 = stop-loss disabled
        p = prices.get(ticker)
        if p is None:
            continue

        if position < 0:
            # NO position
            qty = abs(position)
            bid = p.get("no_bid")
            bid_dollars = p.get("no_bid_dollars")
            if bid is None:
                continue
            if bid > stop_loss_cents:
                continue
            side = "no"
        else:
            # YES position
            qty = position
            bid = p.get("yes_bid")
            bid_dollars = p.get("yes_bid_dollars")
            if bid is None:
                continue
            if bid > stop_loss_cents:
                continue
            side = "yes"

        structure = p.get("price_level_structure", "linear_cent")
        use_dollars = False
        sell_price_dollars = ""
        sell_price = max(1, min(99, bid))
        display_price: int | float = sell_price

        if _supports_deci_cent_at_price(structure, bid) and bid_dollars:
            try:
                sell_val = float(bid_dollars)
                sell_price_dollars = f"{max(0.01, min(0.99, sell_val)):.4f}"
                display_price = round(sell_val * 1000) / 10
                use_dollars = True
            except (ValueError, TypeError):
                pass

        if dry_run:
            sold.add(ticker)
            sold_orders.append((ticker, side.upper(), qty, display_price, "dry-run"))
            continue

        try:
            if use_dollars:
                order = client.create_order(
                    ticker=ticker,
                    side=side,
                    action="sell",
                    count=qty,
                    price_dollars=sell_price_dollars,
                )
            else:
                order = client.create_order(
                    ticker=ticker,
                    side=side,
                    action="sell",
                    count=qty,
                    price_cents=sell_price,
                )
            sold.add(ticker)
            sold_orders.append((ticker, side.upper(), qty, display_price, order.get("order_id", "?")))
        except Exception:
            log.exception("Stop-loss order failed for %s", ticker)

    return sold, sold_orders
