"""Stop-loss monitor: fetch positions, fetch prices, sell YES/NO when price <= stop_loss_cents."""
import logging
from typing import Dict, List, Tuple

import requests

from .orders import place_sell_no, place_sell_yes

# 4-tuple: (yes_bid, yes_ask, no_bid, no_ask) in cents
BidAsk4 = Tuple[int, int, int, int]

logger = logging.getLogger(__name__)

# Chunk size for batch GET /markets (Kalshi accepts multiple tickers per request)
_MARKET_BATCH_CHUNK = 20


def _price_to_cents(price_dollars: any) -> int:
    """Convert API price (dollars string or number) to cents. If value is 0-1, treat as dollars; if 1-100, treat as cents."""
    if price_dollars is None:
        return 0
    try:
        v = float(str(price_dollars).strip())
    except (ValueError, TypeError):
        return 0
    if v <= 0:
        return 0
    # Kalshi can return dollars (0.70) or legacy cents (70); treat 0 < v <= 1 as dollars, v > 1 as cents
    if 0 < v <= 1:
        return int(round(v * 100))
    return int(round(v))


def get_positions(client) -> Tuple[List[Tuple[str, int]], List[Tuple[str, int]]]:
    """Call GET /portfolio/positions once; return (yes_positions, no_positions)."""
    r = client.get("/portfolio/positions")
    r.raise_for_status()
    data = r.json()
    positions = data.get("market_positions") or []
    yes_out: List[Tuple[str, int]] = []
    no_out: List[Tuple[str, int]] = []
    for p in positions:
        pos = p.get("position") or 0
        if isinstance(p.get("position_fp"), str):
            try:
                pos = int(float(p["position_fp"]))
            except (ValueError, TypeError):
                pass
        ticker = p.get("ticker")
        if not ticker:
            continue
        if pos > 0:
            yes_out.append((ticker, pos))
        elif pos < 0:
            no_out.append((ticker, abs(pos)))
    return (yes_out, no_out)


def _parse_bid_ask(m: dict) -> BidAsk4:
    """From a market dict, return (yes_bid, yes_ask, no_bid, no_ask) in cents. Derive NO from YES if missing."""
    yb = _price_to_cents(m.get("yes_bid_dollars") or m.get("yes_bid"))
    ya = _price_to_cents(m.get("yes_ask_dollars") or m.get("yes_ask"))
    nb = _price_to_cents(m.get("no_bid_dollars") or m.get("no_bid"))
    na = _price_to_cents(m.get("no_ask_dollars") or m.get("no_ask"))
    # Binary: no_ask = 100 - yes_bid, no_bid = 100 - yes_ask if not provided
    if not na and yb:
        na = 100 - yb
    if not nb and ya:
        nb = 100 - ya
    return (yb, ya, nb, na)


def get_market_bid_ask_batch(client, tickers: List[str]) -> Dict[str, BidAsk4]:
    """
    Get (yes_bid, yes_ask, no_bid, no_ask) for multiple markets via GET /markets?tickers=...
    Returns dict ticker -> (yes_bid, yes_ask, no_bid, no_ask) in cents. Missing/errors get (0,0,0,0).
    """
    out: Dict[str, BidAsk4] = {}
    empty: BidAsk4 = (0, 0, 0, 0)
    for i in range(0, len(tickers), _MARKET_BATCH_CHUNK):
        chunk = tickers[i : i + _MARKET_BATCH_CHUNK]
        tickers_param = ",".join(chunk)
        try:
            r = client.get("/markets", params={"tickers": tickers_param})
            r.raise_for_status()
            data = r.json()
            for m in data.get("markets") or []:
                t = m.get("ticker")
                if t:
                    out[t] = _parse_bid_ask(m)
        except Exception as e:
            logger.warning("Batch market fetch failed for chunk: %s", e)
            for t in chunk:
                if t not in out:
                    out[t] = empty
    for t in tickers:
        if t not in out:
            out[t] = empty
    return out


def run_stop_loss(
    client,
    stop_loss_cents: int,
    dry_run: bool = False,
) -> None:
    """
    Fetch YES and NO positions, fetch prices for those markets, and place sell orders
    when YES bid <= stop_loss_cents (sell YES) or NO bid <= stop_loss_cents (sell NO).
    """
    try:
        positions_yes, positions_no = get_positions(client)
    except requests.exceptions.HTTPError as e:
        if e.response is not None and e.response.status_code == 401:
            logger.warning("Stop-loss skipped: 401 Unauthorized (check API key and demo vs production).")
            return
        raise
    if not positions_yes and not positions_no:
        return

    all_tickers = [t for t, _ in positions_yes] + [t for t, _ in positions_no]
    bid_ask = get_market_bid_ask_batch(client, all_tickers)

    for ticker, count in positions_yes:
        try:
            yb, ya, nb, na = bid_ask.get(ticker, (0, 0, 0, 0))
            if yb > stop_loss_cents:
                continue
            if not yb:
                logger.warning("Stop loss triggered but YES bid is missing for ticker=%s, skipping sell", ticker)
                continue
            sell_price = min(max(yb, 1), 99)
            logger.info(
                "Stop loss triggered (YES): ticker=%s price_cents=%s <= %s, selling at %sc",
                ticker, yb, stop_loss_cents, sell_price,
            )
            place_sell_yes(client, ticker, count, sell_price, dry_run=dry_run)
        except Exception as e:
            logger.exception("Stop loss failed for YES ticker=%s: %s", ticker, e)

    for ticker, count in positions_no:
        try:
            yb, ya, nb, na = bid_ask.get(ticker, (0, 0, 0, 0))
            if nb > stop_loss_cents:
                continue
            if not nb:
                logger.warning("Stop loss triggered but NO bid is missing for ticker=%s, skipping sell", ticker)
                continue
            sell_price = min(max(nb, 1), 99)
            logger.info(
                "Stop loss triggered (NO): ticker=%s price_cents=%s <= %s, selling at %sc",
                ticker, nb, stop_loss_cents, sell_price,
            )
            place_sell_no(client, ticker, count, sell_price, dry_run=dry_run)
        except Exception as e:
            logger.exception("Stop loss failed for NO ticker=%s: %s", ticker, e)
