"""Place limit buy/sell YES and NO at the ask (when bid >= entry_min and spread <= max_spread); stop loss for both sides."""
import logging
import uuid
from typing import Any, Optional

logger = logging.getLogger(__name__)


def place_buy_yes(
    client,
    ticker: str,
    count: int,
    yes_price_cents: int,
    dry_run: bool = False,
) -> Optional[dict]:
    """
    Place a limit buy YES order. Returns order dict on success (with order_id), None on skip, raises on unexpected error.
    Handles 409 (duplicate client_order_id), 400 (bad request), 429 (rate limit with backoff hint).
    """
    if dry_run:
        logger.info("[DRY RUN] Would place buy YES ticker=%s count=%s yes_price_cents=%s", ticker, count, yes_price_cents)
        return {"order_id": "dry-run", "status": "dry_run"}

    path = "/portfolio/orders"
    payload = {
        "ticker": ticker,
        "action": "buy",
        "side": "yes",
        "type": "limit",
        "yes_price": yes_price_cents,
        "count": count,
        "client_order_id": str(uuid.uuid4()),
    }

    r = client.post(path, payload)

    if r.status_code == 201:
        data = r.json()
        order = data.get("order") or data
        logger.info("Order placed: ticker=%s order_id=%s", ticker, order.get("order_id"))
        return order

    if r.status_code == 409:
        logger.warning("Duplicate client_order_id for ticker=%s", ticker)
        return None
    if r.status_code == 400:
        logger.warning("Bad request for ticker=%s: %s", ticker, r.text)
        return None
    if r.status_code == 429:
        logger.warning("Rate limited (429) for ticker=%s", ticker)
        r.raise_for_status()

    if r.status_code == 404:
        logger.warning("404 Not Found for ticker=%s (market may not exist or endpoint path may have changed).", ticker)
        return None

    r.raise_for_status()
    return None


def place_buy_no(
    client,
    ticker: str,
    count: int,
    no_price_cents: int,
    dry_run: bool = False,
) -> Optional[dict]:
    """Place a limit buy NO order. Same as place_buy_yes but side 'no' and no_price."""
    if dry_run:
        logger.info("[DRY RUN] Would place buy NO ticker=%s count=%s no_price_cents=%s", ticker, count, no_price_cents)
        return {"order_id": "dry-run", "status": "dry_run"}

    path = "/portfolio/orders"
    payload = {
        "ticker": ticker,
        "action": "buy",
        "side": "no",
        "type": "limit",
        "no_price": no_price_cents,
        "count": count,
        "client_order_id": str(uuid.uuid4()),
    }

    r = client.post(path, payload)

    if r.status_code == 201:
        data = r.json()
        order = data.get("order") or data
        logger.info("Order placed (NO): ticker=%s order_id=%s", ticker, order.get("order_id"))
        return order

    if r.status_code == 409:
        logger.warning("Duplicate client_order_id for ticker=%s", ticker)
        return None
    if r.status_code == 400:
        logger.warning("Bad request for ticker=%s: %s", ticker, r.text)
        return None
    if r.status_code == 429:
        logger.warning("Rate limited (429) for ticker=%s", ticker)
        r.raise_for_status()

    if r.status_code == 404:
        logger.warning("404 Not Found for ticker=%s (market may not exist or endpoint path may have changed).", ticker)
        return None

    r.raise_for_status()
    return None


def place_sell_yes(
    client,
    ticker: str,
    count: int,
    yes_price_cents: int,
    dry_run: bool = False,
) -> Optional[dict]:
    """
    Place a limit sell YES order (to close position). Returns order dict on success.
    """
    if dry_run:
        logger.info("[DRY RUN] Would place sell YES ticker=%s count=%s yes_price_cents=%s", ticker, count, yes_price_cents)
        return {"order_id": "dry-run-sell", "status": "dry_run"}

    path = "/portfolio/orders"
    payload = {
        "ticker": ticker,
        "action": "sell",
        "side": "yes",
        "type": "limit",
        "yes_price": yes_price_cents,
        "count": count,
        "client_order_id": str(uuid.uuid4()),
    }

    r = client.post(path, payload)

    if r.status_code == 201:
        data = r.json()
        order = data.get("order") or data
        logger.info("Sell order placed: ticker=%s order_id=%s count=%s", ticker, order.get("order_id"), count)
        return order

    if r.status_code == 409:
        logger.warning("Duplicate client_order_id for sell ticker=%s", ticker)
        return None
    if r.status_code == 400:
        logger.warning("Bad request for sell ticker=%s: %s", ticker, r.text)
        return None
    if r.status_code == 429:
        logger.warning("Rate limited (429) for sell ticker=%s", ticker)
        r.raise_for_status()

    if r.status_code == 404:
        logger.warning("404 Not Found for sell ticker=%s (market may not exist or endpoint path may have changed).", ticker)
        return None

    r.raise_for_status()
    return None


def place_sell_no(
    client,
    ticker: str,
    count: int,
    no_price_cents: int,
    dry_run: bool = False,
) -> Optional[dict]:
    """Place a limit sell NO order (to close NO position). Same as place_sell_yes but side 'no' and no_price."""
    if dry_run:
        logger.info("[DRY RUN] Would place sell NO ticker=%s count=%s no_price_cents=%s", ticker, count, no_price_cents)
        return {"order_id": "dry-run-sell-no", "status": "dry_run"}

    path = "/portfolio/orders"
    payload = {
        "ticker": ticker,
        "action": "sell",
        "side": "no",
        "type": "limit",
        "no_price": no_price_cents,
        "count": count,
        "client_order_id": str(uuid.uuid4()),
    }

    r = client.post(path, payload)

    if r.status_code == 201:
        data = r.json()
        order = data.get("order") or data
        logger.info("Sell order placed (NO): ticker=%s order_id=%s count=%s", ticker, order.get("order_id"), count)
        return order

    if r.status_code == 409:
        logger.warning("Duplicate client_order_id for sell NO ticker=%s", ticker)
        return None
    if r.status_code == 400:
        logger.warning("Bad request for sell NO ticker=%s: %s", ticker, r.text)
        return None
    if r.status_code == 429:
        logger.warning("Rate limited (429) for sell NO ticker=%s", ticker)
        r.raise_for_status()

    if r.status_code == 404:
        logger.warning("404 Not Found for sell NO ticker=%s (market may not exist or endpoint path may have changed).", ticker)
        return None

    r.raise_for_status()
    return None
