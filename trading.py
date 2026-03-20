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

def build_no_candidates(
    prices: dict[str, dict],
    candidates: list[tuple[str, str, dict]],
    cooldown_tickers: set[str],
) -> list[tuple[str, str, int, dict]]:
    """Filter candidates to those meeting the strict entry rule.

    candidates: list of (ticker, event_ticker, params) with per-category params.
    Returns [(ticker, event_ticker, no_bid, params)] for qualified markets.
    """
    qualified: list[tuple[str, str, int, dict]] = []
    for ticker, event_ticker, params in candidates:
        if ticker in cooldown_tickers:
            log.debug("Skip %s — in cooldown", ticker)
            continue
        p = prices.get(ticker)
        if p is None:
            continue
        no_ask = p.get("no_ask")
        no_bid = p.get("no_bid")
        if no_ask is None or no_bid is None:
            continue
        entry_min = params.get("entry_price")
        max_spread = params.get("max_spread", 99)
        min_open_interest = params.get("min_open_interest")
        if no_ask != entry_min:
            continue
        if no_ask - no_bid > max_spread:
            continue
        if no_bid < 1 or no_bid >= 100:
            continue
        oi = p.get("open_interest", 0) or 0
        if min_open_interest is not None and oi < min_open_interest:
            log.debug("Skip %s — open_interest %d < %d", ticker, oi, min_open_interest)
            continue
        qualified.append((ticker, event_ticker, no_bid, params))
    log.info("%d candidates qualify for NO entry", len(qualified))
    return qualified


# ---------------------------------------------------------------------------
# Bet sizing
# ---------------------------------------------------------------------------

def compute_order_sizes(
    balance_cents: int,
    qualified: list[tuple[str, str, int, dict]],
    current_no_tickers: set[str],
    max_pct: float,
    max_positions: int,
    min_contracts: int,
    max_contracts: int,
) -> list[tuple[str, str, int, dict]]:
    """Decide how many contracts to buy for each qualified market.

    Returns [(ticker, event_ticker, count, params)].
    """
    available_slots = max_positions - len(current_no_tickers)
    if available_slots <= 0:
        log.info("No open position slots (max %d reached)", max_positions)
        return []

    orders: list[tuple[str, str, int, dict]] = []
    for ticker, event_ticker, no_bid, params in qualified:
        if len(orders) >= available_slots:
            break
        if ticker in current_no_tickers:
            continue
        allocation = balance_cents * max_pct
        if no_bid <= 0:
            continue
        count = int(allocation // no_bid)
        count = max(min_contracts, min(max_contracts, count))
        orders.append((ticker, event_ticker, count, params))
    log.info("Sized %d entry orders", len(orders))
    return orders


# ---------------------------------------------------------------------------
# Order placement
# ---------------------------------------------------------------------------

def place_entry_orders(
    client,
    orders_to_place: list[tuple[str, str, int, dict]],
    prices: dict[str, dict],
    dry_run: bool,
) -> list[tuple[str, int, int, str]]:
    """Place limit-buy NO orders for each entry, with a final re-check.
    Each order includes params for per-category entry_price and max_spread.
    Returns [(ticker, count, price_cents, order_id), ...] for placed orders."""
    placed: list[tuple[str, int, int, str]] = []
    for ticker, event_ticker, count, params in orders_to_place:
        p = prices.get(ticker)
        if p is None:
            log.warning("No price data for %s on re-check; skipping", ticker)
            continue
        no_ask = p.get("no_ask")
        no_bid = p.get("no_bid")
        if no_ask is None or no_bid is None:
            log.warning("Missing bid/ask for %s on re-check; skipping", ticker)
            continue
        entry_min = params.get("entry_price")
        max_spread = params.get("max_spread", 99)
        if no_ask != entry_min:
            log.info("NO ask for %s moved to %s; skipping", ticker, no_ask)
            continue
        if no_ask - no_bid > max_spread:
            log.info("Spread widened for %s; skipping", ticker)
            continue
        if no_bid < 1 or no_bid >= 100:
            log.info("NO bid %s out of range for %s; skipping", no_bid, ticker)
            continue

        limit_price = max(1, min(99, no_bid))
        if dry_run:
            placed.append((ticker, count, limit_price, "dry-run"))
            continue

        try:
            order = client.create_order(
                ticker=ticker,
                side="no",
                action="buy",
                count=count,
                price_cents=limit_price,
            )
            placed.append((ticker, count, limit_price, order.get("order_id", "?")))
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
) -> tuple[set[str], list[tuple[str, str, int, int, str]]]:
    """Sell positions whose bid has dropped to or below the stop-loss threshold.

    stop_loss_map: ticker -> stop_loss_cents (per-market).
    Returns (sold_tickers, [(ticker, side, qty, price_cents, order_id), ...])."""
    sold: set[str] = set()
    sold_orders: list[tuple[str, str, int, int, str]] = []
    for pos in positions:
        ticker = pos.get("ticker", "")
        position = pos.get("position", 0)
        if position == 0:
            continue
        stop_loss_cents = stop_loss_map.get(ticker)
        if stop_loss_cents is None:
            continue
        p = prices.get(ticker)
        if p is None:
            continue

        if position < 0:
            # NO position
            qty = abs(position)
            bid = p.get("no_bid")
            if bid is None:
                continue
            if bid > stop_loss_cents:
                continue
            sell_price = max(1, min(99, bid))
            side = "no"
        else:
            # YES position
            qty = position
            bid = p.get("yes_bid")
            if bid is None:
                continue
            if bid > stop_loss_cents:
                continue
            sell_price = max(1, min(99, bid))
            side = "yes"

        if dry_run:
            sold.add(ticker)
            sold_orders.append((ticker, side.upper(), qty, sell_price, "dry-run"))
            continue

        try:
            order = client.create_order(
                ticker=ticker,
                side=side,
                action="sell",
                count=qty,
                price_cents=sell_price,
            )
            sold.add(ticker)
            sold_orders.append((ticker, side.upper(), qty, sell_price, order.get("order_id", "?")))
        except Exception:
            log.exception("Stop-loss order failed for %s", ticker)

    return sold, sold_orders
