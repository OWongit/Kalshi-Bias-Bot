"""
Download historical 1-minute candlestick data for all settled markets of the
given series tickers / category slugs and save as CSVs.

Usage:
    python calibration/fetch_data.py KXNCAAMBGAME KXETH15M
    python calibration/fetch_data.py KXETH15M,KXBTC15M,kxnbagame
    python calibration/fetch_data.py -t KXETH15M -t KXBTC15M
    # Multiple targets in one run: all CSVs + one _markets.csv under
    # calibration/past_data/<TICKER1>_<TICKER2>_...
    python calibration/fetch_data.py KXNCAAMBGAME [SLUG2 ...]
    python calibration/fetch_data.py KXETH15M
    python calibration/fetch_data.py kxnbagame
    python calibration/fetch_data.py kxmlbgame
    python calibration/fetch_data.py kxnhlgame
    python calibration/fetch_data.py kxeplgame
    python calibration/fetch_data.py kxbtcd
    python calibration/fetch_data.py football
    python calibration/fetch_data.py culture
    python calibration/fetch_data.py kxeplgame
    python calibration/fetch_data.py kxlolgame
    python calibration/fetch_data.py tennis
    python calibration/fetch_data.py mma

    python calibration/fetch_data.py https://kalshi.com/category/sports/tennis
    python calibration/fetch_data.py KXNCAAMBGAME --force   # re-download existing
"""

import argparse
import csv
import logging
import os
import re
import sys
import time
from datetime import datetime, timezone
from urllib.parse import urlparse

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


def _expand_fetch_targets(tokens: list[str]) -> list[str]:
    """Split comma-separated tokens and drop blanks (order preserved)."""
    out: list[str] = []
    for raw in tokens:
        if not raw:
            continue
        for part in re.split(r"\s*,\s*", raw.strip()):
            if part:
                out.append(part)
    return out


def _storage_slug(target: str) -> str:
    """Folder name for a fetch target, preserving the existing past_data layout."""
    raw = target.strip()
    if raw.lower().startswith(("http://", "https://")):
        parsed = urlparse(raw)
        path = parsed.path.strip("/")
        if path.lower().startswith("category/"):
            parts = [p for p in path[len("category/"):].split("/") if p]
            if parts:
                return parts[-1].upper()
    return raw.strip("/").upper()


def _combined_past_data_dir_basename(targets: list[str]) -> str:
    """Single ``past_data`` subfolder name listing every CLI target (storage-normalized)."""
    parts = [_storage_slug(t) for t in targets]
    return "_".join(parts)


def _write_manifest(slug_dir: str, all_markets_meta: list[dict]) -> str:
    """Write ``_markets.csv`` for the current dataset and return its path."""
    manifest_path = os.path.join(slug_dir, "_markets.csv")
    with open(manifest_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=MARKETS_CSV_COLUMNS)
        writer.writeheader()
        writer.writerows(all_markets_meta)
    return manifest_path


def _count_market_csv_files(slug_dir: str) -> int:
    """How many market candle ``*.csv`` files are in *slug_dir* (excludes ``_markets.csv``)."""
    n = 0
    try:
        for name in os.listdir(slug_dir):
            if not name.endswith(".csv"):
                continue
            if name == "_markets.csv":
                continue
            n += 1
    except OSError:
        pass
    return n


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


def download_slug(
    client: KalshiClient,
    slug: str,
    cutoff_ts: str,
    force: bool,
    min_candles: int = 0,
    slug_dir: str | None = None,
    write_manifest: bool = True,
) -> tuple[bool, list[dict]]:
    """Download candlestick CSVs for every settled market of *slug*.

    *slug_dir* — if set, CSVs go here (used when fetching several targets into one folder).
    *write_manifest* — if False, caller must write ``_markets.csv`` (e.g. merged multi-fetch).

    Returns (interrupted, markets_meta_rows_for_this_slug).
    """
    if slug_dir is None:
        slug_dir = os.path.join(PAST_DATA_DIR, _storage_slug(slug))
    os.makedirs(slug_dir, exist_ok=True)

    cutoff_unix = _iso_to_unix(cutoff_ts) if isinstance(cutoff_ts, str) else int(cutoff_ts)

    all_markets_meta: list[dict] = []
    added_file_count = 0
    interrupted = False

    try:
        series_tickers = discover_series_for_slug(client, slug)
        if not series_tickers:
            log.warning("No series found for slug '%s'", slug)
            return False, []

        for series_ticker in series_tickers:
            markets = fetch_settled_markets(client, series_ticker)

            for m in markets:
                ticker = m["ticker"]
                csv_path = os.path.join(slug_dir, f"{ticker}.csv")
                existed_before = os.path.exists(csv_path)

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

                if min_candles > 0 and len(candles) < min_candles:
                    log.info(
                        "Skipping %s: %d candles < --min-candles %d",
                        ticker, len(candles), min_candles,
                    )
                    time.sleep(0.05)
                    continue

                with open(csv_path, "w", newline="", encoding="utf-8") as f:
                    writer = csv.writer(f)
                    writer.writerow(CANDLE_CSV_COLUMNS)
                    for c in candles:
                        writer.writerow(flatten_candle(c))

                if (not existed_before) and len(candles) > 0:
                    added_file_count += 1
                file_count = _count_market_csv_files(slug_dir)
                log.info(
                    "File count: %d | Saved %d candles -> %s",
                    file_count, len(candles), csv_path,
                )
                time.sleep(0.05)
    except KeyboardInterrupt:
        interrupted = True
        log.warning("Interrupted while processing '%s'; writing partial _markets.csv", slug)
    finally:
        if write_manifest:
            manifest_path = _write_manifest(slug_dir, all_markets_meta)
            log.info("Wrote manifest with %d markets -> %s", len(all_markets_meta), manifest_path)
        folder_csvs = _count_market_csv_files(slug_dir)
        log.info(
            "File count: %d CSV(s) in folder (new this run with >0 candles: %d)",
            folder_csvs, added_file_count,
        )

    return interrupted, all_markets_meta


def main():
    parser = argparse.ArgumentParser(
        description="Download historical candlestick data for settled markets."
    )
    parser.add_argument(
        "slugs", nargs="*",
        help="Series tickers, category slugs, or Kalshi category URLs (space- or comma-separated)",
    )
    parser.add_argument(
        "-t", "--ticker",
        action="append",
        default=None,
        metavar="SERIES_OR_SLUG",
        help="Same as positional targets; repeatable (e.g. -t KXETH15M -t KXBTC15M)",
    )
    parser.add_argument(
        "--force", action="store_true",
        help="Re-download even if CSV already exists",
    )
    parser.add_argument(
        "--min-candles", type=int, default=0, metavar="N",
        help="Discard markets with fewer than N candles (CSV not written)",
    )
    args = parser.parse_args()

    from_flag = list(args.ticker) if args.ticker else []
    targets = _expand_fetch_targets(list(args.slugs) + from_flag)
    if not targets:
        parser.error(
            "Provide at least one series ticker, slug, or URL "
            "(positional args and/or -t/--ticker).",
        )

    private_key = config.load_private_key()
    client = KalshiClient(config.BASE_URL, config.API_KEY_ID, private_key)

    log.info("Fetching historical cutoff timestamps …")
    cutoff = client.get_historical_cutoff()
    cutoff_ts = cutoff.get("market_settled_ts", "2000-01-01T00:00:00Z")
    log.info("Cutoff market_settled_ts: %s", cutoff_ts)

    interrupted = False
    if len(targets) == 1:
        slug = targets[0]
        log.info("=== Processing slug: %s ===", slug)
        interrupted, _ = download_slug(
            client, slug, cutoff_ts, args.force, args.min_candles,
        )
    else:
        combined_basename = _combined_past_data_dir_basename(targets)
        combined_dir = os.path.join(PAST_DATA_DIR, combined_basename)
        log.info(
            "=== Multi-target fetch: %d target(s) -> folder %s ===",
            len(targets), combined_basename,
        )
        accumulated_meta: list[dict] = []
        for slug in targets:
            log.info("--- Processing slug: %s ---", slug)
            intr, meta = download_slug(
                client,
                slug,
                cutoff_ts,
                args.force,
                args.min_candles,
                slug_dir=combined_dir,
                write_manifest=False,
            )
            accumulated_meta.extend(meta)
            if intr:
                interrupted = True
                break
        manifest_path = _write_manifest(combined_dir, accumulated_meta)
        log.info(
            "Wrote combined manifest with %d markets -> %s",
            len(accumulated_meta),
            manifest_path,
        )

    if interrupted:
        log.warning("Stopped early after interruption.")
    else:
        log.info("Done.")


if __name__ == "__main__":
    main()
