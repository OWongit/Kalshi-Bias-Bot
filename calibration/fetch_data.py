"""
Download historical 1-minute candlestick data for all settled markets of the
given series tickers / category slugs and save as CSVs.

Usage:
    python calibration/fetch_data.py KXNCAAMBGAME [SLUG2 ...]
    python calibration/fetch_data.py kxbtc15m
    python calibration/fetch_data.py kxeth15m
    python calibration/fetch_data.py kxxrp15m
    python calibration/fetch_data.py kxsol15m
    python calibration/fetch_data.py kxdoge15m
    python calibration/fetch_data.py kxhype15m
    python calibration/fetch_data.py kxbnb15m
    python calibration/fetch_data.py KXNCAAMBGAME --force   # re-download existing
"""

import argparse
import csv
import logging
import os
import sys
import time
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import config
from api_client import KalshiClient
from discovery import discover_series_for_slug

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("fetch_data")

PAST_DATA_DIR = os.path.join(os.path.dirname(__file__), "past_data")

CANDLE_CSV_COLUMNS = [
    "end_period_ts",
    "yes_bid_open", "yes_bid_high", "yes_bid_low", "yes_bid_close",
    "yes_ask_open", "yes_ask_high", "yes_ask_low", "yes_ask_close",
    "price_open", "price_high", "price_low", "price_close", "price_mean",
    "volume", "open_interest",
]

MARKETS_CSV_COLUMNS = ["ticker", "result", "open_time", "close_time"]


def _dollars_to_cents(val):
    if val is None:
        return ""
    try:
        return round(float(val) * 100)
    except (ValueError, TypeError):
        return ""


def _extract_ohlc_cents(dist: dict | None, use_dollars: bool = True) -> tuple:
    """Return (open, high, low, close) in cents from a bid/ask distribution."""
    if dist is None:
        return ("", "", "", "")
    if use_dollars:
        return (
            _dollars_to_cents(dist.get("open_dollars", dist.get("open"))),
            _dollars_to_cents(dist.get("high_dollars", dist.get("high"))),
            _dollars_to_cents(dist.get("low_dollars", dist.get("low"))),
            _dollars_to_cents(dist.get("close_dollars", dist.get("close"))),
        )
    return (
        dist.get("open", ""),
        dist.get("high", ""),
        dist.get("low", ""),
        dist.get("close", ""),
    )


def _extract_price_cents(dist: dict | None, use_dollars: bool = True) -> tuple:
    """Return (open, high, low, close, mean) in cents from a price distribution."""
    if dist is None:
        return ("", "", "", "", "")
    if use_dollars:
        return (
            _dollars_to_cents(dist.get("open_dollars", dist.get("open"))),
            _dollars_to_cents(dist.get("high_dollars", dist.get("high"))),
            _dollars_to_cents(dist.get("low_dollars", dist.get("low"))),
            _dollars_to_cents(dist.get("close_dollars", dist.get("close"))),
            _dollars_to_cents(dist.get("mean_dollars", dist.get("mean"))),
        )
    return (
        dist.get("open", ""),
        dist.get("high", ""),
        dist.get("low", ""),
        dist.get("close", ""),
        dist.get("mean", ""),
    )


def _volume_val(candle: dict) -> str:
    """Extract volume, preferring the fp string (parse to int)."""
    fp = candle.get("volume_fp") or candle.get("volume")
    if fp is None:
        return ""
    try:
        return int(float(str(fp)))
    except (ValueError, TypeError):
        return fp


def _oi_val(candle: dict) -> str:
    fp = candle.get("open_interest_fp") or candle.get("open_interest")
    if fp is None:
        return ""
    try:
        return int(float(str(fp)))
    except (ValueError, TypeError):
        return fp


def _iso_to_unix(iso_str: str) -> int:
    """Parse an ISO-8601 datetime string to a Unix timestamp."""
    dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
    return int(dt.timestamp())


def flatten_candle(candle: dict) -> list:
    """Convert a single candlestick dict into a flat CSV row."""
    yb = _extract_ohlc_cents(candle.get("yes_bid"))
    ya = _extract_ohlc_cents(candle.get("yes_ask"))
    pr = _extract_price_cents(candle.get("price"))
    return [
        candle.get("end_period_ts", ""),
        *yb, *ya, *pr,
        _volume_val(candle),
        _oi_val(candle),
    ]


def fetch_settled_markets(client: KalshiClient, series_ticker: str) -> list[dict]:
    """Paginate all settled markets for a series ticker."""
    markets: list[dict] = []
    cursor = ""
    while True:
        page, cursor = client.get_markets(
            series_ticker=series_ticker, status="settled", cursor=cursor
        )
        markets.extend(page)
        if not cursor:
            break
    log.info("Found %d settled markets for series %s", len(markets), series_ticker)
    return markets


def download_slug(client: KalshiClient, slug: str, cutoff_ts: str, force: bool):
    """Download candlestick CSVs for every settled market of *slug*."""
    series_tickers = discover_series_for_slug(client, slug.upper())
    if not series_tickers:
        log.warning("No series found for slug '%s'", slug)
        return

    slug_dir = os.path.join(PAST_DATA_DIR, slug.upper())
    os.makedirs(slug_dir, exist_ok=True)

    cutoff_unix = _iso_to_unix(cutoff_ts) if isinstance(cutoff_ts, str) else int(cutoff_ts)

    all_markets_meta: list[dict] = []

    for series_ticker in series_tickers:
        markets = fetch_settled_markets(client, series_ticker)

        for m in markets:
            ticker = m["ticker"]
            csv_path = os.path.join(slug_dir, f"{ticker}.csv")

            result = m.get("result", "")
            open_time = m.get("open_time", "")
            close_time = m.get("close_time", "")

            all_markets_meta.append({
                "ticker": ticker,
                "result": result,
                "open_time": open_time,
                "close_time": close_time,
            })

            if os.path.exists(csv_path) and not force:
                log.debug("Skipping %s (CSV exists)", ticker)
                continue

            try:
                start_ts = _iso_to_unix(open_time) if open_time else 0
                end_ts = _iso_to_unix(close_time) if close_time else int(
                    datetime.now(timezone.utc).timestamp()
                )
            except Exception:
                log.warning("Cannot parse timestamps for %s; skipping", ticker)
                continue

            settle_ts = 0
            if m.get("settlement_ts"):
                try:
                    settle_ts = _iso_to_unix(m["settlement_ts"])
                except Exception:
                    pass

            use_historical_first = bool(settle_ts and settle_ts < cutoff_unix)
            candles = None

            if use_historical_first:
                try:
                    candles = client.get_historical_candlesticks(
                        ticker, start_ts, end_ts, period_interval=1,
                    )
                except Exception:
                    log.debug("Historical endpoint failed for %s; trying live", ticker)

            if candles is None:
                try:
                    candles = client.get_candlesticks(
                        series_ticker, ticker, start_ts, end_ts, period_interval=1,
                    )
                except Exception:
                    log.debug("Live endpoint failed for %s; trying historical", ticker)

            if candles is None and not use_historical_first:
                try:
                    candles = client.get_historical_candlesticks(
                        ticker, start_ts, end_ts, period_interval=1,
                    )
                except Exception:
                    pass

            if candles is None:
                log.warning("Could not fetch candlesticks for %s from either endpoint", ticker)
                time.sleep(0.05)
                continue

            with open(csv_path, "w", newline="", encoding="utf-8") as f:
                writer = csv.writer(f)
                writer.writerow(CANDLE_CSV_COLUMNS)
                for c in candles:
                    writer.writerow(flatten_candle(c))

            log.info("Saved %d candles -> %s", len(candles), csv_path)
            time.sleep(0.05)

    manifest_path = os.path.join(slug_dir, "_markets.csv")
    with open(manifest_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=MARKETS_CSV_COLUMNS)
        writer.writeheader()
        writer.writerows(all_markets_meta)
    log.info("Wrote manifest with %d markets -> %s", len(all_markets_meta), manifest_path)


def main():
    parser = argparse.ArgumentParser(
        description="Download historical candlestick data for settled markets."
    )
    parser.add_argument(
        "slugs", nargs="+",
        help="Series tickers or category slugs (e.g. KXNCAAMBGAME)",
    )
    parser.add_argument(
        "--force", action="store_true",
        help="Re-download even if CSV already exists",
    )
    args = parser.parse_args()

    private_key = config.load_private_key()
    client = KalshiClient(config.BASE_URL, config.API_KEY_ID, private_key)

    log.info("Fetching historical cutoff timestamps …")
    cutoff = client.get_historical_cutoff()
    cutoff_ts = cutoff.get("market_settled_ts", "2000-01-01T00:00:00Z")
    log.info("Cutoff market_settled_ts: %s", cutoff_ts)

    for slug in args.slugs:
        log.info("=== Processing slug: %s ===", slug)
        download_slug(client, slug, cutoff_ts, args.force)

    log.info("Done.")


if __name__ == "__main__":
    main()
