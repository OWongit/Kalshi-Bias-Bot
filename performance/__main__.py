"""
Kalshi Performance Dashboard

Fetches fills and settlements from the Kalshi API, computes cumulative P&L,
and generates an interactive HTML dashboard.

Usage:
    Edit the configuration section below, then from the repo root run:
    python -m performance
"""

import csv
import math
import os
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone, tzinfo
from zoneinfo import ZoneInfo

_PERF_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(_PERF_DIR)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import plotly.colors as plc
import plotly.graph_objects as go
from plotly.subplots import make_subplots
from scipy.stats import norm

import config
from api_client import KalshiClient

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
START_DATE = "2026-4-13"      # Only include activity from this date onward (YYYY-MM-DD)
OFFSET_DOLLARS = 0            # Shift the P&L baseline (e.g. initial deposit adjustment)
PORTFOLIO_ALLOCATION = 0.04 # The % of your account allocated per trade (0.10 = 10%)
STATISTICAL_EDGE_MARGIN_PCT = 0.75  # Slight buffer over 1% for the proportion edge test
OUTPUT_FILE = os.path.join(_PERF_DIR, "performance.html")
EVENTS_CSV_FILE = os.path.join(_PERF_DIR, "performance_events.csv")
# One full market ticker per line (same as ``ticker`` in ``performance_events.csv``).
# Lines starting with # are comments. Matched tickers are still written to the CSV
# with ``ignored=yes`` but are omitted from ``performance.html``.
PERFORMANCE_IGNORE_PATH = os.path.join(_PERF_DIR, "performance_ignore.txt")
# Series to exclude from both the dashboard and performance stats (case-insensitive).
# Events with a matching series are still written to the CSV with ``ignored=yes``.
IGNORE_SERIES: list[str] = ["kxnbagame"]
# Local timezone for chart times, daily P&L buckets, and hourly frequency (IANA).
# Must match your wall clock (e.g. America/Los_Angeles if times were ~7h ahead of Pacific).
HOURLY_FREQUENCY_TIMEZONE = "America/Los_Angeles"

# Series to render in the bottom "entry vs profit" panels (one panel per entry,
# matched case-insensitively against ``e["series"]`` from ``_extract_series``).
# ``ENTRY_PROFIT_REFERENCE_CENTS`` is the nominal entry price in cents/contract
# for that series; points with ``entry_cents < reference - 3`` are hidden.
# ENTRY_PROFIT_SERIES: list[str] = ["KXBTC15M", "KXETH15M", "KXSOL15M", "KXXRP15M"]
# ENTRY_PROFIT_REFERENCE_CENTS: list[float] = [97.0, 98.0, 98.0, 97.0]
ENTRY_PROFIT_SERIES: list[str] = []
ENTRY_PROFIT_REFERENCE_CENTS: list[float] = []


def load_performance_ignore_tickers(path: str) -> set[str]:
    """Load ignore tickers (one per line, ``#`` comments, blank lines skipped)."""
    out: set[str] = set()
    if not os.path.isfile(path):
        return out
    with open(path, encoding="utf-8") as f:
        for line in f:
            raw = line.strip()
            if not raw or raw.startswith("#"):
                continue
            out.add(raw.upper())
    return out


def _is_ignored_ticker(ticker: str | None, ignored: set[str]) -> bool:
    """True if *ticker* matches an ignore line (exact, or event is a longer Kalshi suffix).

    Fills/settlements often use the full market ticker (e.g. ``...-26APR131115-15`` for
    a 15m window). Ignore lines may omit the trailing ``-15`` / ``-30`` segment; those
    still count as a match when the event ticker starts with ``<line>-``.
    """
    if not ticker or not ignored:
        return False
    t = ticker.strip().upper()
    if t in ignored:
        return True
    for stem in ignored:
        if not stem:
            continue
        if t.startswith(stem + "-"):
            return True
    return False


def _build_ignore_series_set() -> set[str]:
    """Normalise ``IGNORE_SERIES`` config list into upper-cased set."""
    return {s.strip().upper() for s in IGNORE_SERIES if s.strip()}


def _is_ignored_event(
    event: dict,
    ignore_tickers: set[str],
    ignore_series: set[str],
) -> bool:
    """True when an event should be excluded (by ticker *or* by series)."""
    if _is_ignored_ticker(event.get("ticker"), ignore_tickers):
        return True
    if ignore_series:
        ser = str(event.get("series", "")).strip().upper()
        if ser in ignore_series:
            return True
    return False


def _resolve_display_timezone() -> tuple[tzinfo, str]:
    """IANA zones need the ``tzdata`` package on Windows. Fallback: UTC."""
    key = HOURLY_FREQUENCY_TIMEZONE.strip()
    if key.upper() == "UTC":
        return timezone.utc, "UTC"
    try:
        return ZoneInfo(key), key
    except Exception:
        print(
            f"Warning: Could not load timezone {key!r} (install: pip install tzdata). "
            "Using UTC for all chart times.",
            file=sys.stderr,
        )
        return timezone.utc, "UTC"


def _ts_for_display(ts: datetime, tz: tzinfo) -> datetime:
    """Kalshi timestamps are UTC; convert for plotting and calendar-day grouping."""
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(tz)


# ---------------------------------------------------------------------------
# API helpers
# ---------------------------------------------------------------------------

def _config_date_to_utc(s: str) -> datetime:
    """Parse start date (``YYYY-MM-DD`` or single-digit month/day) at midnight UTC."""
    raw = s.strip()
    try:
        return datetime.strptime(raw, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        parts = raw.split("-")
        if len(parts) == 3:
            y, mo, d = int(parts[0]), int(parts[1]), int(parts[2])
            return datetime(y, mo, d, tzinfo=timezone.utc)
        raise


def _parse_ts(start_date: str) -> int:
    """Convert YYYY-MM-DD string to Unix timestamp."""
    return int(_config_date_to_utc(start_date).timestamp())


def _format_elapsed_since(start: datetime, end: datetime) -> str:
    """Human-readable wall-clock span (e.g. ``12d 5h 3m``)."""
    if end < start:
        return "0m"
    delta = end - start
    days = delta.days
    hours, rem = divmod(delta.seconds, 3600)
    mins, _ = divmod(rem, 60)
    if days > 0:
        return f"{days}d {hours}h {mins}m"
    if hours > 0:
        return f"{hours}h {mins}m"
    return f"{mins}m"


def _fetch_paginated(client: KalshiClient, path: str, key: str,
                     min_ts: int | None = None) -> list[dict]:
    """Generic paginated fetch for fills/settlements."""
    items: list[dict] = []
    cursor = ""
    while True:
        params: dict = {"limit": 200}
        if min_ts is not None:
            params["min_ts"] = min_ts
        if cursor:
            params["cursor"] = cursor
        data = client.get(path, params=params)
        items.extend(data.get(key, []))
        cursor = data.get("cursor", "")
        if not cursor:
            break
        time.sleep(0.15)
    return items


def fetch_all_fills(client: KalshiClient, min_ts: int) -> list[dict]:
    """Fetch fills from both live and historical endpoints."""
    live = _fetch_paginated(client, "/portfolio/fills", "fills", min_ts)
    historical = []
    try:
        historical = _fetch_paginated(client, "/historical/fills", "fills", min_ts)
    except Exception:
        pass
    seen_ids = {f.get("trade_id") for f in live if f.get("trade_id")}
    for h in historical:
        if h.get("trade_id") not in seen_ids:
            live.append(h)
    return live


def fetch_all_settlements(client: KalshiClient, min_ts: int) -> list[dict]:
    """Fetch settlements from both live and historical endpoints."""
    live = _fetch_paginated(client, "/portfolio/settlements", "settlements", min_ts)
    historical = []
    try:
        historical = _fetch_paginated(client, "/historical/settlements", "settlements", min_ts)
    except Exception:
        pass
    seen = {(s.get("ticker"), s.get("settled_time")) for s in live}
    for h in historical:
        if (h.get("ticker"), h.get("settled_time")) not in seen:
            live.append(h)
    return live


def fetch_portfolio_balance(client: KalshiClient) -> float:
    """Fetch the current portfolio balance from Kalshi in dollars."""
    try:
        res = client.get("/portfolio/balance")
        if "balance" in res:
            return float(res["balance"]) / 100.0
        return 100.0  # Fallback
    except Exception as e:
        print(f"Warning: Failed to fetch balance, defaulting to $100.0 ({e})")
        return 100.0


# ---------------------------------------------------------------------------
# Data processing
# ---------------------------------------------------------------------------

def _extract_series(ticker: str) -> str:
    """KXBTC15M-26MAR200130-30 -> KXBTC15M"""
    return ticker.split("-", 1)[0] if "-" in ticker else ticker


def _fill_price_cents(fill: dict) -> float:
    """Extract the fill price in cents based on side, preferring dollars fields."""
    side = fill.get("side", "")
    if side == "no":
        dollars_key, legacy_key = "no_price_dollars", "no_price"
    else:
        dollars_key, legacy_key = "yes_price_dollars", "yes_price"
    v = fill.get(dollars_key)
    if v is not None:
        try:
            return float(v) * 100
        except (ValueError, TypeError):
            pass
    v = fill.get(legacy_key)
    if v is not None:
        return float(v)
    return 0.0


def _fill_count(fill: dict) -> int:
    c = fill.get("count")
    if c is not None:
        return int(c)
    fp = fill.get("count_fp")
    if fp is not None:
        try:
            return int(float(str(fp)))
        except (ValueError, TypeError):
            pass
    return 0


def _fill_fee_cents(fill: dict) -> float:
    """Exchange fee for this fill in cents (``fee_cost`` is fixed-point dollars string)."""
    v = fill.get("fee_cost")
    if v is None or v == "":
        return 0.0
    try:
        return float(str(v)) * 100.0
    except (ValueError, TypeError):
        return 0.0


def _taker_role_summary(taker_flags: set[bool]) -> str:
    """How buy fills split on ``is_taker``: ``true`` / ``false`` / ``mixed`` / empty."""
    if not taker_flags:
        return ""
    if taker_flags == {True}:
        return "true"
    if taker_flags == {False}:
        return "false"
    return "mixed"


def _parse_fill_ts(fill: dict) -> datetime:
    """Parse the fill timestamp."""
    ts = fill.get("ts") or fill.get("created_time")
    if ts is None:
        return datetime.now(timezone.utc)
    if isinstance(ts, (int, float)):
        return datetime.fromtimestamp(ts, tz=timezone.utc)
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except Exception:
        return datetime.now(timezone.utc)


def _parse_settlement_ts(s: dict) -> datetime:
    ts = s.get("settled_time")
    if ts is None:
        return datetime.now(timezone.utc)
    if isinstance(ts, (int, float)):
        return datetime.fromtimestamp(ts, tz=timezone.utc)
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except Exception:
        return datetime.now(timezone.utc)


def build_pnl_events(fills: list[dict], settlements: list[dict]) -> list[dict]:
    """Build realized P&L events: one entry per settled market or sell fill.

    Per-ticker ``fee_cost_cents`` sums all fill ``fee_cost`` values (cents).
    ``is_taker`` summarizes **buy** fills only: ``true`` / ``false`` / ``mixed``.
    ``buy_filled_ts`` is the earliest buy-fill timestamp for the ticker (UTC).

    Drops events with ``cost_cents == 0``.
    """
    ticker_cost: dict[str, float] = defaultdict(float)  # total spent (cents)
    ticker_buy_count: dict[str, int] = defaultdict(int)  # total contracts bought
    ticker_sell_revenue: dict[str, float] = defaultdict(float)
    ticker_side_counts: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    ticker_fee_cents: dict[str, float] = defaultdict(float)
    ticker_buy_is_taker: dict[str, set[bool]] = defaultdict(set)
    ticker_first_buy_ts: dict[str, datetime] = {}

    for f in fills:
        ticker = f.get("ticker") or f.get("market_ticker") or ""
        action = f.get("action", "")
        price = _fill_price_cents(f)
        count = _fill_count(f)
        ticker_fee_cents[ticker] += _fill_fee_cents(f)
        if action == "buy":
            ticker_cost[ticker] += price * count
            ticker_buy_count[ticker] += count
            fill_side = f.get("side", "no")
            ticker_side_counts[ticker][fill_side] += count
            if f.get("is_taker") is not None:
                ticker_buy_is_taker[ticker].add(bool(f["is_taker"]))
            if ticker:
                buy_ts = _parse_fill_ts(f)
                prev = ticker_first_buy_ts.get(ticker)
                if prev is None or buy_ts < prev:
                    ticker_first_buy_ts[ticker] = buy_ts
        elif action == "sell":
            ticker_sell_revenue[ticker] += price * count

    events: list[dict] = []

    settled_tickers: set[str] = set()
    for s in settlements:
        ticker = s.get("ticker", "")
        ts = _parse_settlement_ts(s)
        revenue = 0
        rev_dollars = s.get("revenue_dollars")
        if rev_dollars is not None:
            try:
                revenue = round(float(rev_dollars) * 100)
            except (ValueError, TypeError):
                pass
        if revenue == 0:
            revenue = s.get("revenue", 0) or 0

        buy_count = ticker_buy_count.get(ticker, 0)
        max_revenue = buy_count * 100
        if revenue > max_revenue:
            revenue = max_revenue

        cost = ticker_cost.get(ticker, 0)
        sell_rev = ticker_sell_revenue.get(ticker, 0)
        pnl = revenue + sell_rev - cost
        settled_tickers.add(ticker)

        sides = ticker_side_counts.get(ticker, {})
        dominant_side = max(sides, key=sides.get) if sides else "no"
        events.append({
            "ts": ts,
            "ticker": ticker,
            "series": _extract_series(ticker),
            "type": "settlement",
            "pnl_cents": pnl,
            "cost_cents": cost,
            "buy_count": ticker_buy_count.get(ticker, 0),
            "fill_side": dominant_side,
            "fee_cost_cents": round(ticker_fee_cents.get(ticker, 0.0), 6),
            "is_taker": _taker_role_summary(ticker_buy_is_taker.get(ticker, set())),
            "buy_filled_ts": ticker_first_buy_ts.get(ticker),
        })

    for f in fills:
        ticker = f.get("ticker") or f.get("market_ticker") or ""
        if ticker in settled_tickers:
            continue
        action = f.get("action", "")
        if action != "sell":
            continue
        price = _fill_price_cents(f)
        count = _fill_count(f)
        ts = _parse_fill_ts(f)
        sell_cost = ticker_cost.get(ticker, 0)
        sides = ticker_side_counts.get(ticker, {})
        dominant_side = max(sides, key=sides.get) if sides else "no"
        events.append({
            "ts": ts,
            "ticker": ticker,
            "series": _extract_series(ticker),
            "type": "sell",
            "pnl_cents": price * count,
            "cost_cents": sell_cost,
            "buy_count": ticker_buy_count.get(ticker, 0),
            "fill_side": dominant_side,
            "fee_cost_cents": round(ticker_fee_cents.get(ticker, 0.0), 6),
            "is_taker": _taker_role_summary(ticker_buy_is_taker.get(ticker, set())),
            "buy_filled_ts": ticker_first_buy_ts.get(ticker),
        })

    events.sort(key=lambda e: e["ts"])
    events = [e for e in events if (e.get("cost_cents") or 0) != 0]
    return events


def _csv_derived_contract_cost(cost_cents: float, buy_count: int) -> float | str:
    """cost_cents / buy_count (¢ per contract); empty if buy_count is 0."""
    if buy_count <= 0:
        return ""
    return round(cost_cents / buy_count, 6)


def _csv_derived_percent_gain(cost_cents: float, pnl_cents: float) -> float | str:
    """(((cost + pnl) / cost) - 1) * 100; empty if cost is 0."""
    if cost_cents == 0:
        return ""
    return round(((cost_cents + pnl_cents) / cost_cents - 1.0) * 100.0, 6)


def write_events_csv(
    events: list[dict],
    path: str,
    ignore_tickers: set[str] | None = None,
    ignore_series: set[str] | None = None,
) -> None:
    """Omits rows with buy_count == 0. Adds contract_cost and percent_gain.

    Excludes columns: ts (UTC), type, buy_filled_ts (UTC); the localized
    ``ts_local`` and ``buy_filled_ts_local`` are written instead. Full market
    ``ticker`` is written after ``series``, then ``ignored`` (``yes``/``no``
    vs ``performance_ignore.txt`` and ``IGNORE_SERIES``). Includes
    ``fee_cost_cents`` and ``is_taker`` from ``build_pnl_events``.
    """
    if ignore_tickers is None:
        ignore_tickers = load_performance_ignore_tickers(PERFORMANCE_IGNORE_PATH)
    if ignore_series is None:
        ignore_series = _build_ignore_series_set()
    display_tz, _ = _resolve_display_timezone()
    rows = [e for e in events if int(e.get("buy_count") or 0) != 0]
    derived = ("contract_cost", "percent_gain")
    _csv_skip_keys = frozenset({"ts", "type", "buy_filled_ts", *derived})
    leading = ("ts_local", "buy_filled_ts_local", "series", "ticker", "ignored")
    if rows:
        keys: set[str] = set()
        for e in rows:
            keys |= e.keys()
        keys -= _csv_skip_keys
        rest = sorted(k for k in keys if k not in leading)
        fieldnames = list(leading) + rest + list(derived)
    else:
        fieldnames = [
            *leading,
            "pnl_cents",
            "cost_cents",
            "buy_count",
            "fill_side",
            "fee_cost_cents",
            "is_taker",
            "contract_cost",
            "percent_gain",
        ]
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for e in rows:
            ts = e["ts"]
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            buy_ts = e.get("buy_filled_ts")
            if isinstance(buy_ts, datetime):
                if buy_ts.tzinfo is None:
                    buy_ts = buy_ts.replace(tzinfo=timezone.utc)
                buy_ts_local = _ts_for_display(buy_ts, display_tz).isoformat()
            else:
                buy_ts_local = ""
            row: dict[str, object] = {
                "ts_local": _ts_for_display(ts, display_tz).isoformat(),
                "buy_filled_ts_local": buy_ts_local,
            }
            cost = float(e.get("cost_cents") or 0)
            pnl = float(e.get("pnl_cents") or 0)
            bc = int(e.get("buy_count") or 0)
            for k in fieldnames[1:]:
                if k in row:
                    continue
                if k == "contract_cost":
                    row[k] = _csv_derived_contract_cost(cost, bc)
                elif k == "percent_gain":
                    row[k] = _csv_derived_percent_gain(cost, pnl)
                elif k == "ignored":
                    row[k] = (
                        "yes" if _is_ignored_event(e, ignore_tickers, ignore_series) else "no"
                    )
                else:
                    row[k] = e.get(k, "")
            w.writerow(row)
    print(f"Events CSV saved to {path} ({len(rows)} rows, buy_count != 0)")


def format_win_rate_proportion_edge_line(
    expected_win_rate_pct: float, 
    wins: int, 
    n: int, 
    margin_pct: float = 0.01
) -> str:
    """
    One-tailed Z-test for proportions with an added margin.
    Tests if the actual win rate is significantly greater than (expected + margin).
    """
    if n <= 0:
        return (
            "Statistical Edge: Z-score: n/a | P-value: n/a "
            "(Not Significant at alpha 0.05)"
        )

    # Convert percentages to decimals
    p_base = expected_win_rate_pct / 100.0
    margin = margin_pct / 100.0
    
    # Our new null hypothesis proportion (p0) is the base + margin
    p0 = p_base + margin
    
    # Clamp p0 to prevent math errors near 0 or 1
    p0 = min(max(p0, 1e-15), 1.0 - 1e-15)
    
    phat = wins / n
    
    # Calculate Standard Error using the buffered null proportion
    se = math.sqrt(p0 * (1.0 - p0) / n)
    
    if se <= 0.0:
        return (
            "Statistical Edge: Z-score: n/a | P-value: n/a "
            "(Not Significant at alpha 0.05)"
        )

    # Calculate Z-score: (Actual - (Expected + Buffer)) / SE
    z = (phat - p0) / se
    
    # One-tailed p-value (probability of seeing a result this high by chance)
    p_value = float(norm.sf(z))
    
    verdict = "Significant" if p_value < 0.05 else "Not Significant"
    
    margin_str = f" with {margin_pct}% margin"
    
    return (
        f"Statistical Edge{margin_str}: Z-score: {z:.4f} | P-value: {p_value:.6f} "
        f"({verdict} at alpha 0.05)"
    )


def _sortino_annualized(
    returns: list[float],
    start_ts: datetime,
    end_ts: datetime,
    mar: float = 0.0,
) -> float | None:
    """Annualized Sortino ratio (MAR defaults to 0).

    Returns None when fewer than 2 observations or zero downside deviation.
    Annualization: sqrt(n / T_years) where T_years is calendar span of events.
    """
    n = len(returns)
    if n < 2:
        return None
    mean = sum(returns) / n
    downside_sq = sum(min(0.0, r - mar) ** 2 for r in returns) / n
    downside_dev = math.sqrt(downside_sq)
    if downside_dev < 1e-12:
        return None
    t_years = max((end_ts - start_ts).total_seconds() / (365.25 * 24 * 3600), 1e-9)
    return ((mean - mar) / downside_dev) * math.sqrt(n / t_years)


def _fmt_sortino(val: float | None) -> str:
    return "n/a" if val is None else f"{val:.2f}"


# ---------------------------------------------------------------------------
# Chart building
# ---------------------------------------------------------------------------

def build_dashboard(
    events: list[dict],
    offset_dollars: float,
    current_balance: float,
    alloc_pct: float,
    output_file: str,
    ignore_tickers: set[str] | None = None,
    ignore_series: set[str] | None = None,
) -> None:
    if not events:
        print("No data to plot.")
        sys.exit(0)

    if ignore_tickers is None:
        ignore_tickers = load_performance_ignore_tickers(PERFORMANCE_IGNORE_PATH)
    if ignore_series is None:
        ignore_series = _build_ignore_series_set()
    n_all = len(events)
    events = [e for e in events if not _is_ignored_event(e, ignore_tickers, ignore_series)]
    n_ignored = n_all - len(events)
    if n_ignored:
        print(
            f"  Excluding {n_ignored} event(s) matching ignore list(s) from dashboard",
        )
    if not events:
        print("No data to plot (all events excluded by ignore lists).")
        sys.exit(0)

    display_tz, display_tz_label = _resolve_display_timezone()

    def _chart_net_pnl_cents(e: dict) -> float:
        """Gross ``pnl_cents`` minus ``fee_cost_cents`` for $ / return charts only (CSV unchanged).

        Win-rate counts still use gross ``pnl_cents`` so a fee-drained trade stays a win if
        settlement P&L was positive.
        """
        gross = float(e.get("pnl_cents") or 0)
        fees = float(e.get("fee_cost_cents") or 0)
        return gross - fees

    timestamps = []
    cumulative_pnl = []
    running = offset_dollars * 100 
    for e in events:
        running += _chart_net_pnl_cents(e)
        timestamps.append(_ts_for_display(e["ts"], display_tz))
        cumulative_pnl.append(running / 100)

    daily_pnl: dict[str, float] = defaultdict(float)
    for e in events:
        day = _ts_for_display(e["ts"], display_tz).strftime("%Y-%m-%d")
        daily_pnl[day] += _chart_net_pnl_cents(e) / 100
    daily_dates = sorted(daily_pnl.keys())
    daily_values = [daily_pnl[d] for d in daily_dates]
    daily_colors = ["#2ecc71" if v >= 0 else "#e74c3c" for v in daily_values]

    pct_timestamps = []
    pct_values = []
    running_pnl_for_pct = 0.0
    running_cost_for_pct = 0.0
    markets_with_cost = 0
    for e in events:
        running_pnl_for_pct += _chart_net_pnl_cents(e)
        cost = e.get("cost_cents", 0)
        running_cost_for_pct += cost
        if cost > 0:
            markets_with_cost += 1
        pct_timestamps.append(_ts_for_display(e["ts"], display_tz))
        avg_cost = (running_cost_for_pct / markets_with_cost) if markets_with_cost > 0 else 0
        pct = (running_pnl_for_pct / avg_cost * 100) if avg_cost > 0 else 0.0
        pct_values.append(pct)

    # -- Sortino: per-trade return on cost (for "Cumulative Return %" plot) --
    pct_sortino_returns: list[float] = []
    for e in events:
        cost = float(e.get("cost_cents") or 0)
        if cost > 0:
            pct_sortino_returns.append(_chart_net_pnl_cents(e) / cost)

    # -- Sortino: path-dependent equity returns (for "Cumulative P&L ($)" plot) --
    # ε = $1 (100 cents) prevents division-by-zero when cumulative equity is near 0
    _EPS_CENTS = 100.0
    pnl_sortino_returns: list[float] = []
    equity = offset_dollars * 100
    for e in events:
        net = _chart_net_pnl_cents(e)
        pnl_sortino_returns.append(net / max(abs(equity), _EPS_CENTS))
        equity += net

    ev_start = events[0]["ts"]
    ev_end = events[-1]["ts"]
    sortino_pnl = _sortino_annualized(pnl_sortino_returns, ev_start, ev_end)
    sortino_pct = _sortino_annualized(pct_sortino_returns, ev_start, ev_end)

    def _position_side(e: dict) -> str:
        s = str(e.get("fill_side", "no")).lower()
        return s if s in ("yes", "no") else "no"

    series_side_trades: dict[tuple[str, str], int] = defaultdict(int)
    series_side_wins: dict[tuple[str, str], int] = defaultdict(int)
    for e in events:
        if e["type"] != "settlement" or e.get("cost_cents", 0) <= 0 or e.get("pnl_cents", 0) == 0:
            continue
        key = (e["series"], _position_side(e))
        series_side_trades[key] += 1
        # Wins from gross settlement P&L — fees do not flip a correct resolution to a loss.
        if float(e.get("pnl_cents") or 0) > 0:
            series_side_wins[key] += 1

    series_side_expected_lists: dict[tuple[str, str], list[float]] = defaultdict(list)
    for e in events:
        if e["type"] == "settlement" and e.get("cost_cents", 0) > 0 and e.get("pnl_cents", 0) != 0:
            if e.get("buy_count", 0) > 0:
                expected_wr = e["cost_cents"] / e["buy_count"]
                series_side_expected_lists[(e["series"], _position_side(e))].append(expected_wr)

    series_with_trades = {k[0] for k in series_side_trades}
    series_best_wr: dict[str, float] = {}
    for ser in series_with_trades:
        wrs = []
        for side in ("yes", "no"):
            t = series_side_trades.get((ser, side), 0)
            if t <= 0:
                continue
            w = series_side_wins.get((ser, side), 0)
            wrs.append(100.0 * w / t)
        series_best_wr[ser] = max(wrs) if wrs else 0.0

    sorted_series_for_plot = sorted(
        series_with_trades,
        key=lambda s: series_best_wr.get(s, 0.0),
        reverse=True,
    )

    series_pnl_totals: dict[str, float] = defaultdict(float)
    series_cost_totals: dict[str, float] = defaultdict(float)
    for e in events:
        ser = e["series"]
        series_pnl_totals[ser] += _chart_net_pnl_cents(e)
        series_cost_totals[ser] += float(e.get("cost_cents") or 0)

    series_pnl_by_row = [series_pnl_totals[s] / 100.0 for s in sorted_series_for_plot]
    series_pct_gain_by_row = [
        (100.0 * series_pnl_totals[s] / series_cost_totals[s]) if series_cost_totals[s] > 0 else 0.0
        for s in sorted_series_for_plot
    ]
    series_pnl_bar_colors = ["#2ecc71" if v >= 0 else "#e74c3c" for v in series_pnl_by_row]
    series_pct_bar_colors = ["#3498db" if v >= 0 else "#e67e22" for v in series_pct_gain_by_row]

    series_names: list[str] = []
    series_values: list[float] = []
    series_trade_counts: list[int] = []
    series_won_counts: list[int] = []
    series_expected_values: list[float] = []
    for ser in sorted_series_for_plot:
        for side in ("yes", "no"):
            key = (ser, side)
            t = series_side_trades.get(key, 0)
            w = series_side_wins.get(key, 0)
            wr = (100.0 * w / t) if t > 0 else 0.0
            wrs_exp = series_side_expected_lists.get(key, [])
            avg_exp = sum(wrs_exp) / len(wrs_exp) if wrs_exp else 0.0
            label = f"{ser} · {side.upper()}"
            series_names.append(label)
            series_values.append(wr)
            series_trade_counts.append(t)
            series_won_counts.append(w)
            series_expected_values.append(avg_exp)

    series_bar_colors = ["#2ecc71" if v >= 50 else "#e74c3c" for v in series_values]

    side_trades: dict[str, int] = defaultdict(int)
    side_wins: dict[str, int] = defaultdict(int)
    for e in events:
        if e["type"] != "settlement" or e.get("cost_cents", 0) <= 0 or e.get("pnl_cents", 0) == 0:
            continue
        fs = e.get("fill_side", "no").upper()
        side_trades[fs] += 1
        if float(e.get("pnl_cents") or 0) > 0:
            side_wins[fs] += 1

    all_sides = sorted(side_trades.keys())
    side_win_rates = {}
    for s in all_sides:
        t = side_trades[s]
        side_win_rates[s] = (side_wins[s] / t * 100) if t > 0 else 0

    if len(all_sides) >= 2:
        total_t = sum(side_trades.values())
        total_w = sum(side_wins.values())
        overall_wr = (total_w / total_t * 100) if total_t > 0 else 0
        side_win_rates["BOTH (avg)"] = overall_wr
        all_sides.append("BOTH (avg)")

    side_values = [side_win_rates[s] for s in all_sides]
    side_bar_colors = ["#2ecc71" if v >= 50 else "#e74c3c" for v in side_values]
    
    side_labels = []
    for s in all_sides:
        if s == "BOTH (avg)":
            side_labels.append(s)
        else:
            t = side_trades.get(s, 0)
            w = side_wins.get(s, 0)
            side_labels.append(f"{s} ({w}/{t})")

    hour_series_count: dict[tuple[int, str], int] = defaultdict(int)
    series_in_trades: set[str] = set()
    for e in events:
        if e["type"] != "settlement" or e.get("cost_cents", 0) <= 0 or e.get("pnl_cents", 0) == 0:
            continue
        local_ts = _ts_for_display(e["ts"], display_tz)
        hour_series_count[(local_ts.hour, e["series"])] += 1
        series_in_trades.add(e["series"])
    hours = list(range(24))
    hour_labels = [f"{h:02d}:00" for h in hours]
    sorted_trade_series = sorted(series_in_trades)
    palette = plc.qualitative.Bold + plc.qualitative.Dark24 + plc.qualitative.Set2
    hourly_series_colors = {s: palette[i % len(palette)] for i, s in enumerate(sorted_trade_series)}

    # -----------------------------------------------------------------------
    # Calculate Trade Returns & Intervals for Account Projection
    # -----------------------------------------------------------------------
    valid_trades = [e for e in events if e.get("cost_cents", 0) > 0 and e.get("type") == "settlement"]
    
    total_trade_return_pct = 0.0
    avg_trade_return_pct = 0.0
    hourly_gain = 0.0
    daily_gain = 0.0
    weekly_gain = 0.0

    proj_hours = 30 * 24
    x_proj_days = list(h / 24.0 for h in range(proj_hours + 1))
    center_proj = [current_balance for _ in x_proj_days]
    upper_proj = [current_balance for _ in x_proj_days]
    lower_proj = [current_balance for _ in x_proj_days]

    if valid_trades:
        # Sum total
        returns = [
            (_chart_net_pnl_cents(e) / e["cost_cents"]) * 100 for e in valid_trades
        ]
        total_trade_return_pct = sum(returns)
        avg_trade_return_pct = total_trade_return_pct / len(valid_trades)

        # Total timespan
        start_ts = min(e["ts"] for e in events)
        end_ts = max(e["ts"] for e in events)
        total_seconds = (end_ts - start_ts).total_seconds()
        total_hours = max(1.0, total_seconds / 3600.0)
        
        # Calculate timeframe gains
        hourly_gain = total_trade_return_pct / total_hours
        daily_gain = hourly_gain * 24
        weekly_gain = hourly_gain * 168

        # --- Calculate Variance and Projection Paths ---
        # Group into integer hourly buckets to calculate std deviation
        hourly_buckets = defaultdict(float)
        for e in valid_trades:
            idx = int((e["ts"] - start_ts).total_seconds() / 3600.0)
            ret = (_chart_net_pnl_cents(e) / e["cost_cents"]) * 100
            hourly_buckets[idx] += ret
            
        sum_sq = sum(val**2 for val in hourly_buckets.values())
        variance = (sum_sq / total_hours) - (hourly_gain**2)
        variance = max(0, variance) # prevent floating point negatives
        sigma_hourly = math.sqrt(variance)

        # Adjust by Portfolio Allocation fraction
        mu_alloc = (hourly_gain / 100.0) * alloc_pct
        sigma_alloc = (sigma_hourly / 100.0) * alloc_pct

        # Apply Geometric Brownian Motion logic for exp compounding
        if 1 + mu_alloc > 0:
            m = math.log(1 + mu_alloc) - (sigma_alloc**2) / 2.0
        else:
            m = 0
            
        center_proj = []
        upper_proj = []
        lower_proj = []
        
        for h in range(proj_hours + 1):
            center_val = current_balance * math.exp((m + (sigma_alloc**2)/2.0) * h)
            upper_val = current_balance * math.exp(m * h + 2.0 * sigma_alloc * math.sqrt(h))
            lower_val = current_balance * math.exp(m * h - 2.0 * sigma_alloc * math.sqrt(h))
            
            center_proj.append(center_val)
            upper_proj.append(upper_val)
            lower_proj.append(lower_val)


    # -----------------------------------------------------------------------
    # Initializing Subplots 
    # -----------------------------------------------------------------------
    entry_profit_series = list(ENTRY_PROFIT_SERIES)
    entry_profit_refs = list(ENTRY_PROFIT_REFERENCE_CENTS)
    if len(entry_profit_refs) != len(entry_profit_series):
        raise ValueError(
            "ENTRY_PROFIT_SERIES and ENTRY_PROFIT_REFERENCE_CENTS must have the same length."
        )
    n_entry_panels = len(entry_profit_series)

    entry_panel_titles = tuple(
        f"{ser}: entry vs net P&L / return (≥ {ref - 3:.0f}¢)"
        for ser, ref in zip(entry_profit_series, entry_profit_refs)
    )
    base_row_heights = [0.12, 0.10, 0.10, 0.12, 0.11, 0.10, 0.12, 0.23]
    entry_row_weight = 0.07
    scale = 1.0 - entry_row_weight * n_entry_panels
    scaled_base_heights = [h * scale for h in base_row_heights]
    row_heights = scaled_base_heights + [entry_row_weight] * n_entry_panels
    specs = [
        [{}],
        [{}],
        [{}],
        [{}],
        [{"secondary_y": True}],
        [{}],
        [{}],
        [{}],
    ] + [[{"secondary_y": True}] for _ in range(n_entry_panels)]

    fig = make_subplots(
        rows=8 + n_entry_panels,
        cols=1,
        subplot_titles=(
            f"Cumulative P&L ($) · Sortino {_fmt_sortino(sortino_pnl)}",
            f"Cumulative Return (%) · Sortino {_fmt_sortino(sortino_pct)}",
            "Daily P&L ($)",
            "Win Rate by Series & side (YES / NO) (%)",
            "P&L ($) & return (%) by series",
            "Win Rate by Side (%)",
            "Trade frequency by hour",
            f"Account Value Projection (Next 30 Days at {alloc_pct*100:.0f}% Alloc)",
            *entry_panel_titles,
        ),
        vertical_spacing=0.032,
        row_heights=row_heights,
        specs=specs,
    )

    fig.add_trace(
        go.Scatter(
            x=timestamps, y=cumulative_pnl,
            mode="lines",
            line=dict(color="#3498db", width=2),
            fill="tozeroy",
            fillcolor="rgba(52, 152, 219, 0.15)",
            name="Cumulative P&L",
            showlegend=False,
        ),
        row=1, col=1,
    )
    fig.add_hline(y=0, line_dash="dash", line_color="gray", opacity=0.5, row=1, col=1)

    if cumulative_pnl:
        _yp_lo = min(cumulative_pnl)
        _yp_hi = max(cumulative_pnl)
        _yp_span = _yp_hi - _yp_lo
        _yp_buf = max(_yp_span * 0.06, 1.0)
        fig.update_yaxes(range=[_yp_lo - _yp_buf, _yp_hi + _yp_buf], row=1, col=1)

    last_pct = pct_values[-1] if pct_values else 0
    pct_line_color = "#2ecc71" if last_pct >= 0 else "#e74c3c"
    pct_fill_color = "rgba(46,204,113,0.15)" if last_pct >= 0 else "rgba(231,76,60,0.15)"
    fig.add_trace(
        go.Scatter(
            x=pct_timestamps, y=pct_values,
            mode="lines",
            line=dict(color=pct_line_color, width=2),
            fill="tozeroy",
            fillcolor=pct_fill_color,
            name="Cumulative Return %",
            showlegend=False,
        ),
        row=2, col=1,
    )
    fig.add_hline(y=0, line_dash="dash", line_color="gray", opacity=0.5, row=2, col=1)

    fig.add_trace(
        go.Bar(
            x=daily_dates, y=daily_values,
            marker_color=daily_colors,
            name="Daily P&L",
            showlegend=False,
        ),
        row=3, col=1,
    )
    fig.add_hline(y=0, line_dash="dash", line_color="gray", opacity=0.5, row=3, col=1)

    fig.add_trace(
        go.Bar(
            y=series_names, x=series_values,
            orientation="h",
            marker_color=series_bar_colors,
            name="Actual Win Rate",
            showlegend=False,
            text=[
                (f"{v:.1f}%<br>{w}/{t}" if t > 0 else "—<br>0 trades")
                for v, w, t in zip(series_values, series_won_counts, series_trade_counts)
            ],
            textposition="inside",
            insidetextanchor="start",
            customdata=list(zip(series_won_counts, series_trade_counts)),
            hovertemplate=(
                "%{y}<br>Win rate: %{x:.1f}%<br>"
                "Won / total: %{customdata[0]} / %{customdata[1]}<extra></extra>"
            ),
        ),
        row=4, col=1,
    )
    
    fig.add_trace(
        go.Scatter(
            x=[None], y=[None],
            mode="lines",
            line=dict(color="#f39c12", width=2, dash="dash"),
            name="Expected Win Rate",
            showlegend=False,
        ),
        row=4, col=1,
    )

    fig.add_trace(
        go.Scatter(
            x=series_expected_values,
            y=series_names,
            mode="markers",
            marker=dict(color="rgba(0,0,0,0)", size=25), 
            showlegend=False,
            hovertemplate="Expected: %{x:.1f}%<extra></extra>"
        ),
        row=4, col=1,
    )

    for i, exp_val in enumerate(series_expected_values):
        fig.add_shape(
            type="line",
            x0=exp_val, x1=exp_val,
            y0=i - 0.4, y1=i + 0.4, 
            line=dict(color="#f39c12", width=2),
            row=4, col=1
        )

    if sorted_series_for_plot:
        fig.add_trace(
            go.Bar(
                x=sorted_series_for_plot,
                y=series_pnl_by_row,
                marker_color=series_pnl_bar_colors,
                showlegend=False,
                text=[f"${v:+,.2f}" for v in series_pnl_by_row],
                textposition="outside",
                cliponaxis=False,
                hovertemplate="%{x}<br>P&L: $%{y:+,.2f}<extra></extra>",
            ),
            row=5,
            col=1,
            secondary_y=False,
        )
        fig.add_trace(
            go.Scatter(
                x=sorted_series_for_plot,
                y=series_pct_gain_by_row,
                mode="markers+text",
                marker=dict(size=14, color=series_pct_bar_colors, line=dict(width=1, color="#ecf0f1")),
                showlegend=False,
                text=[f"{v:+.1f}%" for v in series_pct_gain_by_row],
                textposition="top center",
                textfont=dict(size=11, color="#ecf0f1"),
                cliponaxis=False,
                hovertemplate="%{x}<br>Return: %{y:+.2f}%<extra></extra>",
            ),
            row=5,
            col=1,
            secondary_y=True,
        )
    else:
        fig.add_trace(
            go.Bar(x=["—"], y=[0], showlegend=False, marker_color="#555"),
            row=5,
            col=1,
            secondary_y=False,
        )

    fig.add_trace(
        go.Bar(
            x=side_labels, y=side_values,
            marker_color=side_bar_colors,
            name="Side Win Rate",
            showlegend=False,
            text=[f"{v:.1f}%" for v in side_values], 
            textposition="auto",
        ),
        row=6, col=1,
    )

    if sorted_trade_series:
        for ser in sorted_trade_series:
            y_h = [hour_series_count.get((h, ser), 0) for h in hours]
            fig.add_trace(
                go.Bar(
                    x=hour_labels,
                    y=y_h,
                    name=ser,
                    marker_color=hourly_series_colors[ser],
                    showlegend=True,
                    legendgroup=ser,
                ),
                row=7, col=1,
            )
    else:
        fig.add_trace(
            go.Bar(
                x=hour_labels,
                y=[0] * 24,
                name="No qualifying trades",
                marker_color="#555",
                showlegend=False,
            ),
            row=7, col=1,
        )
    fig.add_hline(y=0, line_dash="dash", line_color="gray", opacity=0.5, row=7, col=1)

    # -----------------------------------------------------------------------
    # PROJECTION PLOT (ROW 8)
    # -----------------------------------------------------------------------
    
    fig.add_trace(
        go.Scatter(
            x=x_proj_days, y=lower_proj,
            mode="lines",
            line=dict(color="rgba(231,76,60,0.5)", width=1),
            name="Lower Bound (95% Confidence)",  # <-- UPDATED LABEL
            showlegend=False,
            legendgroup="projection"
        ),
        row=8, col=1
    )
    
    # 2. Upper Bound (fills the space down to the lower bound)
    fig.add_trace(
        go.Scatter(
            x=x_proj_days, y=upper_proj,
            mode="lines",
            line=dict(color="rgba(46,204,113,0.5)", width=1),
            fill="tonexty", 
            fillcolor="rgba(255,255,255,0.05)",
            name="Upper Bound (95% Confidence)",  # <-- UPDATED LABEL
            showlegend=False,
            legendgroup="projection"
        ),
        row=8, col=1
    )
    
    # 3. Expected Value (Center dash)
    fig.add_trace(
        go.Scatter(
            x=x_proj_days, y=center_proj,
            mode="lines",
            line=dict(color="#f39c12", width=3, dash="dash"),
            name="Expected Value",
            showlegend=False,
            legendgroup="projection"
        ),
        row=8, col=1
    )

    # -----------------------------------------------------------------------
    # ENTRY vs PROFIT PLOTS (rows 9..)
    # -----------------------------------------------------------------------
    for i, (target_series, reference_cents) in enumerate(
        zip(entry_profit_series, entry_profit_refs)
    ):
        row_idx = 9 + i
        target_key = target_series.strip().upper()
        min_entry_cents = reference_cents - 3.0

        entry_sum_usd: dict[float, float] = defaultdict(float)
        entry_sum_pct: dict[float, float] = defaultdict(float)
        entry_trade_counts: dict[float, int] = defaultdict(int)
        for e in events:
            if e.get("type") != "settlement":
                continue
            cost = float(e.get("cost_cents") or 0)
            bc = int(e.get("buy_count") or 0)
            pnl = float(e.get("pnl_cents") or 0)
            if cost <= 0 or bc <= 0 or pnl == 0:
                continue
            if str(e.get("series", "")).strip().upper() != target_key:
                continue
            entry_cents = round(cost / bc, 4)
            if entry_cents < min_entry_cents:
                continue
            net_cents = _chart_net_pnl_cents(e)
            entry_sum_usd[entry_cents] += net_cents / 100.0
            entry_sum_pct[entry_cents] += net_cents / cost * 100.0
            entry_trade_counts[entry_cents] += 1

        entry_x = sorted(entry_sum_usd.keys())
        entry_y_usd = [entry_sum_usd[x] for x in entry_x]
        entry_y_pct = [entry_sum_pct[x] for x in entry_x]
        entry_counts = [entry_trade_counts[x] for x in entry_x]
        usd_colors = ["#2ecc71" if v >= 0 else "#e74c3c" for v in entry_y_usd]
        pct_colors = ["#3498db" if v >= 0 else "#e67e22" for v in entry_y_pct]

        fig.add_trace(
            go.Scatter(
                x=entry_x,
                y=entry_y_usd,
                mode="markers",
                marker=dict(size=9, color=usd_colors, opacity=0.85,
                            line=dict(width=0.5, color="#ecf0f1")),
                name=f"{target_series} net $",
                showlegend=False,
                customdata=entry_counts,
                hovertemplate=(
                    "Entry: %{x:.2f}¢<br>"
                    "Total Net P&L: $%{y:+.2f}<br>"
                    "Trades: %{customdata}<extra></extra>"
                ),
            ),
            row=row_idx, col=1, secondary_y=False,
        )
        fig.add_trace(
            go.Scatter(
                x=entry_x,
                y=entry_y_pct,
                mode="markers",
                marker=dict(size=9, color=pct_colors, opacity=0.85, symbol="diamond",
                            line=dict(width=0.5, color="#ecf0f1")),
                name=f"{target_series} net %",
                showlegend=False,
                customdata=entry_counts,
                hovertemplate=(
                    "Entry: %{x:.2f}¢<br>"
                    "Total Return: %{y:+.2f}%<br>"
                    "Trades: %{customdata}<extra></extra>"
                ),
            ),
            row=row_idx, col=1, secondary_y=True,
        )
        fig.add_hline(y=0, line_dash="dash", line_color="gray", opacity=0.4,
                      row=row_idx, col=1)

    # -----------------------------------------------------------------------
    # Global Stats & Text Formatting
    # -----------------------------------------------------------------------
    total_pnl = cumulative_pnl[-1] if cumulative_pnl else 0
    trades = [e for e in events if e["type"] == "settlement" and e.get("cost_cents", 0) > 0 and e.get("pnl_cents", 0) != 0]
    total_trades = len(trades)
    total_trade_cost = sum(e["cost_cents"] for e in trades)
    avg_total_cost = (total_trade_cost / total_trades / 100) if total_trades > 0 else 0

    total_fee_cents = sum(float(e.get("fee_cost_cents") or 0) for e in trades)
    total_fees = total_fee_cents / 100.0
    avg_fee = (total_fee_cents / total_trades / 100.0) if total_trades > 0 else 0.0

    total_contracts = sum(e.get("buy_count", 0) for e in trades)
    avg_contract_price = (total_trade_cost / total_contracts) if total_contracts > 0 else 0
    expected_win_rate = avg_contract_price

    wins = sum(1 for e in trades if float(e.get("pnl_cents") or 0) > 0)
    actual_win_rate = (wins / total_trades * 100) if total_trades > 0 else 0

    pnl_color = "#2ecc71" if total_pnl >= 0 else "#e74c3c"
    pct_color = "#2ecc71" if last_pct >= 0 else "#e74c3c"
    wr_color = "#2ecc71" if actual_win_rate >= expected_win_rate else "#e74c3c"

    stat_edge_line = format_win_rate_proportion_edge_line(
        expected_win_rate, wins, total_trades, STATISTICAL_EDGE_MARGIN_PCT
    )

    implied_start_balance = current_balance - total_pnl
    if implied_start_balance > 1e-9:
        account_gain_pct = (current_balance / implied_start_balance - 1.0) * 100.0
        account_gain_fmt = f"{account_gain_pct:+.2f}%"
        ag_color = "#2ecc71" if account_gain_pct >= 0 else "#e74c3c"
        account_gain_html = f"<span style='color:{ag_color}'>({account_gain_fmt})</span>"
    else:
        account_gain_fmt = "n/a"
        account_gain_html = f"<span style='color:#b0b0b0'>({account_gain_fmt})</span>"

    start_dt = _config_date_to_utc(START_DATE)
    elapsed_str = _format_elapsed_since(start_dt, datetime.now(timezone.utc))

    fig.update_layout(
        title=dict(
            text=(
                f"Kalshi Bot Performance | Account Value: ${current_balance:,.2f} {account_gain_html} | "
                f"Elapsed: {elapsed_str} | Portfolio Alloc: {alloc_pct*100:.0f}%<br>"
                f"P&L: <span style='color:{pnl_color}'>${total_pnl:+,.2f}</span> | "
                f"Return: <span style='color:{pct_color}'>{last_pct:+.2f}%</span> | "
                f"Avg Cost: ${avg_total_cost:,.2f} | "
                f"Total Fee: ${total_fees:,.4f} | Avg Fee: ${avg_fee:,.4f} | "
                f"Trades: {total_trades}<br>"
                f"Expected Win Rate: {expected_win_rate:.1f}% | "
                f"Actual Win Rate: <span style='color:{wr_color}'>{actual_win_rate:.1f}%</span> "
                f"({wins}/{total_trades})<br>"
                f"<span style='font-size:14px; color:#b0b0b0'>"
                f"Sum Trade Return: {total_trade_return_pct:+.2f}% | "
                f"Avg Trade Return: {avg_trade_return_pct:+.2f}% | "
                f"Hourly Gain: {hourly_gain:+.4f}% | "
                f"Daily Gain: {daily_gain:+.2f}% | "
                f"Weekly Gain: {weekly_gain:+.2f}%"
                f"</span><br>"
                f"<span style='font-size:14px; color:#c8d6e5; display:block; margin-top:12px; padding-top:6px'>"
                f"{stat_edge_line}</span><br><br>"
            ),
            font=dict(size=18),
            y=0.98,
        ),
        height=int(round(2880 / scale)) if scale > 0 else 2880,
        margin=dict(t=195, b=155),
        barmode="stack",
        showlegend=True,
        template="plotly_dark",
        paper_bgcolor="#1a1a2e",
        plot_bgcolor="#16213e",
        font=dict(color="#e0e0e0"),
    )

    fig.update_yaxes(title_text="P&L ($)", row=1, col=1)
    fig.update_yaxes(title_text="Return (%)", row=2, col=1)
    fig.update_yaxes(title_text="P&L ($)", row=3, col=1)
    fig.update_xaxes(title_text="Win Rate (%)", row=4, col=1)
    fig.update_yaxes(autorange="reversed", row=4, col=1)
    fig.update_xaxes(title_text="Series", tickangle=-40, row=5, col=1)
    fig.update_yaxes(title_text="P&L ($)", row=5, col=1, secondary_y=False)
    fig.update_yaxes(title_text="Return (%)", row=5, col=1, secondary_y=True)
    fig.update_yaxes(title_text="Win Rate (%)", row=6, col=1)
    fig.update_yaxes(title_text="Trades", row=7, col=1)
    fig.update_xaxes(title_text="Local hour (start of 1h block)", row=7, col=1)
    
    # Projection formatting
    fig.update_yaxes(title_text="Projected Value ($)", row=8, col=1)
    fig.update_xaxes(title_text="Days from now", row=8, col=1)

    for i in range(n_entry_panels):
        row_idx = 9 + i
        fig.update_xaxes(title_text="Avg entry (¢/contract)", row=row_idx, col=1)
        fig.update_yaxes(title_text="Net P&L ($)", row=row_idx, col=1, secondary_y=False)
        fig.update_yaxes(title_text="Return (%)", row=row_idx, col=1, secondary_y=True)

    # Legend in the gap between trade frequency (row 7) and projection (row 8)
    y6 = fig.layout.yaxis7.domain
    y7 = fig.layout.yaxis8.domain
    gap_lo, gap_hi = y7[1], y6[0]
    # Below mid-gap (toward projection); clamp so it stays above row 7’s plot area
    legend_y = max(gap_lo + 0.012, (gap_lo + gap_hi) / 2 - 0.018)
    fig.update_layout(
        legend=dict(
            orientation="h",
            yanchor="middle",
            y=legend_y,
            xanchor="center",
            x=0.5,
            font=dict(size=11),
        ),
    )

    fig.write_html(output_file, include_plotlyjs="cdn")
    print(f"Dashboard saved to {output_file}\n")
    
    message = (
                f"=== Kalshi Bot Performance ===\n"
                f"Current Account Value: ${current_balance:,.2f}\n"
                f"P&L: ${total_pnl:+,.2f}| "
                f"Return: {last_pct:+.2f}% | "
                f"Avg Cost: ${avg_total_cost:,.2f} | "
                f"Total Fee: ${total_fees:,.2f} | Avg Fee: ${avg_fee:,.2f} | "
                f"Trades: {total_trades}\n"
                f"Expected Win Rate: {expected_win_rate:.1f}% | "
                f"Actual Win Rate: {actual_win_rate:.1f}%"
                f" ({wins}/{total_trades})\n"
                f"Sum Trade Return: {total_trade_return_pct:+.2f}% | "
                f"Avg Trade Return: {avg_trade_return_pct:+.2f}% | "
                f"Hourly Gain: {hourly_gain:+.4f}% | "
                f"Daily Gain: {daily_gain:+.2f}% | "
                f"Weekly Gain: {weekly_gain:+.2f}%\n"
                f"Sortino (P&L): {_fmt_sortino(sortino_pnl)} | "
                f"Sortino (Return): {_fmt_sortino(sortino_pct)}"
            )
    message += "\n" + stat_edge_line
    print(message)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    private_key = config.load_private_key()
    client = KalshiClient(config.BASE_URL, config.API_KEY_ID, private_key)

    # Fetch current balance specifically for the projection model
    current_balance = fetch_portfolio_balance(client)
    print(f"Current Kalshi Balance: ${current_balance:,.2f}")

    min_ts = _parse_ts(START_DATE)

    print(f"Fetching fills since {START_DATE} ...")
    fills = fetch_all_fills(client, min_ts)
    print(f"  {len(fills)} fills")

    print(f"Fetching settlements since {START_DATE} ...")
    settlements = fetch_all_settlements(client, min_ts)
    print(f"  {len(settlements)} settlements")

    events = build_pnl_events(fills, settlements)
    print(f"  {len(events)} total P&L events")

    ignore_set = load_performance_ignore_tickers(PERFORMANCE_IGNORE_PATH)
    ignore_series_set = _build_ignore_series_set()
    if ignore_set:
        print(
            f"  Loaded {len(ignore_set)} ticker(s) from {os.path.basename(PERFORMANCE_IGNORE_PATH)}",
        )
    if ignore_series_set:
        print(
            f"  Ignoring {len(ignore_series_set)} series: {', '.join(sorted(ignore_series_set))}",
        )

    write_events_csv(events, EVENTS_CSV_FILE, ignore_set, ignore_series_set)
    build_dashboard(
        events, OFFSET_DOLLARS, current_balance, PORTFOLIO_ALLOCATION, OUTPUT_FILE,
        ignore_tickers=ignore_set,
        ignore_series=ignore_series_set,
    )


if __name__ == "__main__":
    main()