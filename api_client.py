"""
Authenticated HTTP client for the Kalshi Trade API v2.

Handles RSA-PSS request signing and exposes convenience methods for every
endpoint the bot uses.
"""

import base64
import datetime
import logging
import requests
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding

log = logging.getLogger(__name__)

API_PREFIX = "/trade-api/v2"


# ---------------------------------------------------------------------------
# Price helpers
# ---------------------------------------------------------------------------

def _dollars_to_cents(val):
    """Convert a FixedPointDollars string like '0.9500' to an int in cents."""
    if val is None:
        return None
    try:
        return round(float(val) * 100)
    except (ValueError, TypeError):
        return None


def parse_market_prices(market: dict) -> dict:
    """
    Extract yes_bid, yes_ask, no_bid, no_ask as integer cents from a Market
    response dict.  Prefers the *_dollars fields; falls back to the legacy
    integer fields; infers NO from YES for binary markets when needed.
    """
    def _pick(dollars_key, legacy_key):
        v = _dollars_to_cents(market.get(dollars_key))
        return v if v is not None else market.get(legacy_key)

    yes_bid = _pick("yes_bid_dollars", "yes_bid")
    yes_ask = _pick("yes_ask_dollars", "yes_ask")
    no_bid = _pick("no_bid_dollars", "no_bid")
    no_ask = _pick("no_ask_dollars", "no_ask")

    if no_bid is None and yes_ask is not None:
        no_bid = 100 - yes_ask
    if no_ask is None and yes_bid is not None:
        no_ask = 100 - yes_bid

    oi = market.get("open_interest")
    if oi is None:
        fp = market.get("open_interest_fp")
        if fp is not None:
            try:
                oi = int(float(str(fp)))
            except (ValueError, TypeError):
                pass

    # Raw dollars for deci-cent pricing (Fixed-Point v2)
    no_bid_dollars = market.get("no_bid_dollars")
    if no_bid_dollars is None and market.get("yes_ask_dollars") is not None:
        try:
            no_bid_dollars = f"{1 - float(market['yes_ask_dollars']):.4f}"
        except (ValueError, TypeError):
            pass
    yes_bid_dollars = market.get("yes_bid_dollars")
    if yes_bid_dollars is None and market.get("no_ask_dollars") is not None:
        try:
            yes_bid_dollars = f"{1 - float(market['no_ask_dollars']):.4f}"
        except (ValueError, TypeError):
            pass
    price_level_structure = market.get("price_level_structure", "linear_cent")

    return {
        "yes_bid": yes_bid,
        "yes_ask": yes_ask,
        "no_bid": no_bid,
        "no_ask": no_ask,
        "open_interest": oi,
        "no_bid_dollars": no_bid_dollars,
        "yes_bid_dollars": yes_bid_dollars,
        "price_level_structure": price_level_structure,
    }


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------

class KalshiClient:
    """Thin authenticated wrapper around the Kalshi REST API."""

    def __init__(self, base_url: str, api_key_id: str, private_key):
        self.base_url = base_url.rstrip("/")
        self.api_key_id = api_key_id
        self.private_key = private_key
        self.session = requests.Session()
        self.session.headers["Content-Type"] = "application/json"

    # ---- signing ----------------------------------------------------------

    def _sign(self, timestamp_ms: str, method: str, path: str) -> str:
        path_no_qs = path.split("?")[0]
        message = f"{timestamp_ms}{method}{API_PREFIX}{path_no_qs}".encode("utf-8")
        signature = self.private_key.sign(
            message,
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                salt_length=padding.PSS.DIGEST_LENGTH,
            ),
            hashes.SHA256(),
        )
        return base64.b64encode(signature).decode("utf-8")

    def _headers(self, method: str, path: str) -> dict:
        ts = str(int(datetime.datetime.now(datetime.timezone.utc).timestamp() * 1000))
        return {
            "KALSHI-ACCESS-KEY": self.api_key_id,
            "KALSHI-ACCESS-SIGNATURE": self._sign(ts, method, path),
            "KALSHI-ACCESS-TIMESTAMP": ts,
        }

    # ---- HTTP verbs -------------------------------------------------------

    def _url(self, path: str) -> str:
        return f"{self.base_url}{API_PREFIX}{path}"

    def get(self, path: str, params: dict | None = None):
        resp = self.session.get(
            self._url(path),
            params=params,
            headers=self._headers("GET", path),
        )
        resp.raise_for_status()
        return resp.json()

    def post(self, path: str, json_body: dict | None = None):
        resp = self.session.post(
            self._url(path),
            json=json_body,
            headers=self._headers("POST", path),
        )
        resp.raise_for_status()
        return resp.json()

    def delete(self, path: str):
        resp = self.session.delete(
            self._url(path),
            headers=self._headers("DELETE", path),
        )
        resp.raise_for_status()
        if resp.content:
            return resp.json()
        return None

    # ---- domain helpers ---------------------------------------------------

    def get_balance(self) -> int:
        """Return available balance in cents."""
        data = self.get("/portfolio/balance")
        return data["balance"]

    def get_positions(self, count_filter: str = "position") -> list[dict]:
        """Return all market positions (paginated)."""
        positions: list[dict] = []
        cursor = ""
        while True:
            params: dict = {"limit": 1000, "count_filter": count_filter}
            if cursor:
                params["cursor"] = cursor
            data = self.get("/portfolio/positions", params=params)
            positions.extend(data.get("market_positions", []))
            cursor = data.get("cursor", "")
            if not cursor:
                break
        return positions

    def get_orders(self, status: str = "resting") -> list[dict]:
        """Return open orders (resting by default)."""
        orders: list[dict] = []
        cursor = ""
        while True:
            params: dict = {"limit": 1000, "status": status}
            if cursor:
                params["cursor"] = cursor
            data = self.get("/portfolio/orders", params=params)
            orders.extend(data.get("orders", []))
            cursor = data.get("cursor", "")
            if not cursor:
                break
        return orders

    def get_markets(
        self,
        series_ticker: str | None = None,
        status: str | None = None,
        tickers: str | None = None,
        limit: int = 200,
        cursor: str = "",
    ) -> tuple[list[dict], str]:
        """Fetch one page of markets. Returns (markets_list, next_cursor)."""
        params: dict = {"limit": limit}
        if series_ticker:
            params["series_ticker"] = series_ticker
        if status:
            params["status"] = status
        if tickers:
            params["tickers"] = tickers
        if cursor:
            params["cursor"] = cursor
        data = self.get("/markets", params=params)
        return data.get("markets", []), data.get("cursor", "")

    def get_series_list(self, category: str | None = None) -> list[dict]:
        """Return all series, optionally filtered by category slug."""
        params: dict = {}
        if category:
            params["category"] = category
        data = self.get("/series", params=params)
        return data.get("series", [])

    # ---- candlestick / historical helpers -----------------------------------

    def get_candlesticks(
        self,
        series_ticker: str,
        market_ticker: str,
        start_ts: int,
        end_ts: int,
        period_interval: int = 1,
    ) -> list[dict]:
        """Fetch candlesticks from the live endpoint."""
        path = f"/series/{series_ticker}/markets/{market_ticker}/candlesticks"
        params = {
            "start_ts": start_ts,
            "end_ts": end_ts,
            "period_interval": period_interval,
        }
        data = self.get(path, params=params)
        return data.get("candlesticks", [])

    def get_historical_candlesticks(
        self,
        market_ticker: str,
        start_ts: int,
        end_ts: int,
        period_interval: int = 1,
    ) -> list[dict]:
        """Fetch candlesticks from the historical archive endpoint."""
        path = f"/historical/markets/{market_ticker}/candlesticks"
        params = {
            "start_ts": start_ts,
            "end_ts": end_ts,
            "period_interval": period_interval,
        }
        data = self.get(path, params=params)
        return data.get("candlesticks", [])

    def get_historical_cutoff(self) -> dict:
        """Return the cutoff timestamps separating live from historical data."""
        return self.get("/historical/cutoff")

    # ---- order helpers ----------------------------------------------------

    def create_order(
        self,
        ticker: str,
        side: str,
        action: str,
        count: int,
        price_cents: int | None = None,
        price_dollars: str | None = None,
    ) -> dict:
        """Place an order. Returns the order dict from the API.

        Use price_dollars for deci-cent markets (e.g. "0.9410" for 94.1¢).
        Use price_cents for linear_cent markets. Exactly one must be set.
        """
        body: dict = {
            "ticker": ticker,
            "side": side,
            "action": action,
            "count": count,
        }
        if price_dollars is not None:
            if side == "no":
                body["no_price_dollars"] = price_dollars
            else:
                body["yes_price_dollars"] = price_dollars
        elif price_cents is not None:
            if side == "no":
                body["no_price"] = price_cents
            else:
                body["yes_price"] = price_cents
        else:
            raise ValueError("Either price_cents or price_dollars must be provided")
        data = self.post("/portfolio/orders", json_body=body)
        return data.get("order", data)
