"""
Trading logic: price fetching, entry filtering, bet sizing, order placement,
and stop-loss management.
"""

import logging
import math
import time

import requests

from api_client import parse_market_prices

# Max difference in cents between target limit and resting order (deci-cent tick = 0.1¢).
_LIMIT_CENTS_MATCH_TOL = 0.15

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Price fetching
# ---------------------------------------------------------------------------

def _position_nonzero(pos: dict) -> bool:
    """True if this market position is non-zero (handles int/float/str and position_fp)."""
    p = pos.get("position")
    if p is None and "position_fp" in pos:
        try:
            p = float(str(pos["position_fp"]))
        except (ValueError, TypeError):
            p = 0
    else:
        try:
            p = float(p) if p is not None else 0.0
        except (ValueError, TypeError):
            p = 0.0
    return abs(p) > 1e-12


def tickers_with_open_positions(positions: list[dict]) -> set[str]:
    """Market tickers where we hold any contracts (YES or NO)."""
    out: set[str] = set()
    for pos in positions:
        t = pos.get("ticker", "")
        if t and _position_nonzero(pos):
            out.add(t)
    return out


def cancel_resting_buys_for_position_tickers(
    client,
    open_orders: list[dict],
    tickers_with_position: set[str],
    dry_run: bool,
) -> None:
    """Cancel any resting buy on markets where we already hold a position."""
    if not tickers_with_position:
        return
    for o in open_orders:
        if o.get("action") != "buy" or o.get("status") != "resting":
            continue
        t = o.get("ticker", "")
        if t not in tickers_with_position:
            continue
        oid = o.get("order_id")
        if not oid:
            continue
        if dry_run:
            log.info("[DRY] Would cancel entry buy %s on %s (position exists)", oid, t)
            continue
        try:
            client.cancel_order(oid)
            log.info("Canceled entry buy %s on %s — position already held", oid, t)
        except Exception:
            log.exception("Failed to cancel orphan buy %s for %s", oid, t)


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
# Entry filtering (bid-based)
# ---------------------------------------------------------------------------

def _bid_entry_qualifies(bid: int, ep: int, buy_if_bid_gt_entry: bool) -> bool:
    """``buy_if_bid_gt_entry`` True → ``bid >= ep``; False → ``bid == ep``."""
    if buy_if_bid_gt_entry:
        return bid >= ep
    return bid == ep


def _check_side_entry(
    p: dict, entry_prices: list[int], max_spread: int,
    min_open_interest: int | None, side: str,
    buy_if_bid_gt_entry: bool,
) -> int | None:
    """Check if entry criteria are met for *side* on this market.

    Compares the current best **bid** (integer cents) against ``entry_prices``.
    With ``buy_if_bid_gt_entry=True``: qualifies when ``bid >= ep`` for any ``ep``.
    With ``buy_if_bid_gt_entry=False``: qualifies when ``bid == ep`` for any ``ep``.

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
    if ask - bid > max_spread:
        return None
    if bid < 1 or bid >= 100:
        return None
    oi = p.get("open_interest", 0) or 0
    if min_open_interest is not None and oi < min_open_interest:
        return None

    for ep in entry_prices:
        if _bid_entry_qualifies(bid, ep, buy_if_bid_gt_entry):
            return bid
    return None


def build_candidates(
    prices: dict[str, dict],
    candidates: list[tuple[str, str, dict]],
    cooldown_tickers: set[str],
) -> list[tuple[str, str, int, str, dict]]:
    """Filter candidates to those meeting bid-based entry rules.

    candidates: list of (ticker, event_ticker, params) with per-category params.
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
        buy_gt = params.get("buy_if_bid_gt_entry", False)
        cfg_side = params.get("side", "no")

        if cfg_side == "both":
            sides_to_try = ["no", "yes"]
        else:
            sides_to_try = [cfg_side]

        for s in sides_to_try:
            bid = _check_side_entry(p, entry_prices, max_spread, min_open_interest, s, buy_gt)
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
    tickers_with_position: set[str],
    max_pct: float,
    max_positions: int,
    min_contracts: int,
    max_contracts: int,
) -> list[tuple[str, str, int, str, dict]]:
    """Decide how many contracts to buy for each qualified market.

    Skips markets that already have a position (positions only — resting buy
    orders are managed via cancel-replace in place_entry_orders).
    Returns [(ticker, event_ticker, count, side, params)].
    """
    excluded = tickers_with_position
    available_slots = max_positions - len(excluded)
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
# Limit-price computation (bid + offset)
# ---------------------------------------------------------------------------

def _supports_deci_cent_at_price(structure: str, bid_cents: int) -> bool:
    """True if market supports 0.1¢ tick at this price level."""
    if structure == "deci_cent":
        return True
    if structure == "tapered_deci_cent":
        return bid_cents < 10 or bid_cents > 90
    return False


def _clamp_buy_limit_cents_from_dollars(limit_val: float) -> int:
    """Kalshi binary contracts are 1–99¢ per side; avoid float round-up to 100."""
    cents = int(round(limit_val * 100.0))
    return max(1, min(99, cents))


def _compute_limit_price(
    bid_cents: int,
    bid_dollars: str | None,
    structure: str,
    offset: float,
) -> tuple[int | None, str | None, int | float]:
    """Compute limit price as ``bid + offset`` (cents).

    Returns (price_cents, price_dollars, display_price).
    price_dollars is set only when the book supports deci-cent ticks at this
    price level; otherwise price_cents is used.
    """
    if _supports_deci_cent_at_price(structure, bid_cents) and bid_dollars:
        try:
            bid_val = float(bid_dollars)
            limit_val = bid_val + offset / 100.0
            limit_val = max(0.01, min(0.99, limit_val))
            price_dollars = f"{limit_val:.4f}"
            # Float noise on 0.99*100 can round to 100; clamp cents + display.
            cents_int = _clamp_buy_limit_cents_from_dollars(limit_val)
            display_price = round(limit_val * 1000.0) / 10.0
            display_price = min(99.9, max(0.1, display_price))
            return (cents_int, price_dollars, display_price)
        except (ValueError, TypeError):
            pass
    limit_cents = bid_cents + round(offset)
    limit_cents = max(1, min(99, limit_cents))
    return (limit_cents, None, limit_cents)


# Keep _format_limit_price for stop-loss (unchanged behaviour)
def _format_limit_price(
    no_bid: int,
    no_bid_dollars: str | None,
    structure: str,
    offset_deci_cents: bool,
) -> tuple[int | None, str | None, int | float]:
    """Compute limit price (bid + 0.1¢ or +1¢). Returns (price_cents, price_dollars, display_price)."""
    if offset_deci_cents and _supports_deci_cent_at_price(structure, no_bid):
        if no_bid_dollars:
            try:
                bid_val = float(no_bid_dollars)
                limit_val = min(0.99, bid_val + 0.001)
                price_dollars = f"{limit_val:.4f}"
                display_price = round(limit_val * 1000) / 10
                return (round(limit_val * 100), price_dollars, display_price)
            except (ValueError, TypeError):
                pass
        limit_cents = min(99, no_bid + 1)
        return (limit_cents, None, limit_cents)
    limit_cents = max(1, min(99, no_bid + 1))
    return (limit_cents, None, limit_cents)


# ---------------------------------------------------------------------------
# Order placement (cancel-replace, one resting buy per ticker+side)
# ---------------------------------------------------------------------------

def _find_resting_buy(
    open_orders: list[dict], ticker: str, side: str,
) -> dict | None:
    """Return the first resting buy order for (ticker, side), or None."""
    for o in open_orders:
        if (
            o.get("ticker") == ticker
            and o.get("side") == side
            and o.get("action") == "buy"
            and o.get("status") == "resting"
        ):
            return o
    return None


def _order_limit_cents_display(order: dict, side: str) -> float:
    """Resting order limit in cents (fractional when *_dollars present)."""
    if side == "no":
        d, c = order.get("no_price_dollars"), order.get("no_price")
    else:
        d, c = order.get("yes_price_dollars"), order.get("yes_price")
    if d is not None:
        try:
            return round(float(str(d)) * 1000) / 10
        except (ValueError, TypeError):
            pass
    if c is not None:
        try:
            return float(int(c))
        except (ValueError, TypeError):
            pass
    return 0.0


def _order_remaining_count(order: dict) -> int:
    """Parse remaining_count_fp to int."""
    fp = order.get("remaining_count_fp", "0")
    try:
        return int(float(str(fp)))
    except (ValueError, TypeError):
        return 0


def _resting_buy_matches_target(
    order: dict | None,
    side: str,
    display_price: int | float,
    count: int,
) -> bool:
    """True if this resting buy matches our target limit (cents) and size."""
    if order is None:
        return False
    if _order_remaining_count(order) != count:
        return False
    try:
        lim = _order_limit_cents_display(order, side)
        return abs(lim - float(display_price)) < _LIMIT_CENTS_MATCH_TOL
    except (TypeError, ValueError):
        return False


def _best_bid_snapshot_cents(bid: int, bid_dollars: str | None) -> float:
    """Comparable best-bid level in cents (fractional when *_dollars present)."""
    if bid_dollars:
        try:
            return round(float(bid_dollars) * 1000) / 10
        except (ValueError, TypeError):
            pass
    return float(bid)


def place_entry_orders(
    client,
    orders_to_place: list[tuple[str, str, int, str, dict]],
    prices: dict[str, dict],
    open_orders: list[dict],
    dry_run: bool,
    last_entry_bid_snap: dict[str, float] | None = None,
    tickers_with_position: set[str] | None = None,
) -> list[tuple[str, int, float, float, str, str]]:
    """Place or update limit-buy orders with cancel-replace semantics.

    For each qualified market, computes a target limit from
    ``best_bid + bid_limit_offset``.  If a resting buy already exists at the
    same price and count, it is left alone.  Otherwise the stale order is
    canceled first, then a new one is placed.

    Returns [(ticker, count, bid_cents, order_limit_cents, order_id, side), ...] only for
    orders newly placed this call (nothing returned when bid and resting
    order are unchanged — avoids duplicate console spam each poll).

    ``last_entry_bid_snap`` maps ``"{ticker}:{side}"`` -> last seen best-bid
    snapshot (cents). Pass a persistent dict from the main loop; it is updated
    whenever an order is placed or when we first observe a matching resting
    order at the current bid.

    ``tickers_with_position``: never place or amend bids on these tickers
    (defensive; should match empty ``orders_to_place`` if sizing is correct).
    """
    twp = tickers_with_position or set()
    placed: list[tuple[str, int, float, float, str, str]] = []
    for ticker, event_ticker, count, side, params in orders_to_place:
        if ticker in twp:
            log.debug("Skip %s — already have a position; no entry bid", ticker)
            continue
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
        buy_gt = params.get("buy_if_bid_gt_entry", False)

        qualified = False
        for ep in entry_prices:
            if _bid_entry_qualifies(bid, ep, buy_gt):
                qualified = True
                break
        if not qualified:
            log.info("Bid %s no longer qualifies for %s; skipping", bid, ticker)
            continue
        if ask - bid > max_spread:
            log.info("Spread widened for %s; skipping", ticker)
            continue
        if bid < 1 or bid >= 100:
            log.info("%s bid %s out of range for %s; skipping", side.upper(), bid, ticker)
            continue

        offset = params.get("bid_limit_offset", 0.0)
        structure = p.get("price_level_structure", "linear_cent")
        price_cents, price_dollars, display_price = _compute_limit_price(
            bid, bid_dollars, structure, offset,
        )

        snap = _best_bid_snapshot_cents(bid, bid_dollars)
        key = f"{ticker}:{side}"
        existing = _find_resting_buy(open_orders, ticker, side)

        same_price = _resting_buy_matches_target(
            existing, side, display_price, count,
        )

        unchanged_bid = (
            last_entry_bid_snap is not None
            and key in last_entry_bid_snap
            and math.isclose(last_entry_bid_snap[key], snap, rel_tol=0, abs_tol=1e-6)
        )
        if existing is not None and same_price:
            if unchanged_bid:
                continue
            if last_entry_bid_snap is not None:
                last_entry_bid_snap[key] = snap
            continue

        if existing is not None and not same_price:
            oid = existing["order_id"]
            if dry_run:
                log.info("[DRY] Would cancel stale order %s for %s", oid, ticker)
            else:
                try:
                    client.cancel_order(oid)
                    log.info("Canceled stale order %s for %s", oid, ticker)
                except Exception:
                    log.exception("Failed to cancel order %s for %s", oid, ticker)

        if dry_run:
            order_limit = float(display_price)
            placed.append((ticker, count, snap, order_limit, "dry-run", side))
            if last_entry_bid_snap is not None:
                last_entry_bid_snap[key] = snap
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
            order_limit = _order_limit_cents_display(order, side)
            if order_limit <= 0:
                order_limit = float(display_price)
            placed.append((ticker, count, snap, order_limit, order.get("order_id", "?"), side))
            if last_entry_bid_snap is not None:
                last_entry_bid_snap[key] = snap
        except requests.HTTPError as e:
            if e.response is not None and e.response.status_code == 409:
                log.warning(
                    "HTTP 409 placing order for %s — reconciling resting orders",
                    ticker,
                )
                try:
                    fresh = client.get_orders()
                    ex2 = _find_resting_buy(fresh, ticker, side)
                    if _resting_buy_matches_target(ex2, side, display_price, count):
                        order_limit = _order_limit_cents_display(ex2, side)
                        if order_limit <= 0:
                            order_limit = float(display_price)
                        placed.append(
                            (ticker, count, snap, order_limit, ex2.get("order_id", "?"), side),
                        )
                        if last_entry_bid_snap is not None:
                            last_entry_bid_snap[key] = snap
                    else:
                        log.warning(
                            "409 for %s but no matching resting buy after refresh",
                            ticker,
                        )
                except Exception:
                    log.exception("Failed to reconcile 409 for %s", ticker)
            else:
                log.exception("Failed to place order for %s", ticker)
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
