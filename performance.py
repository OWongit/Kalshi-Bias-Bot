"""
Kalshi Performance Dashboard

Fetches fills and settlements from the Kalshi API, computes cumulative P&L,
and generates an interactive HTML dashboard.

Usage:
    Edit the configuration section below, then run:
    python performance.py
"""

import csv
import sys
import time
import math
from collections import defaultdict
from datetime import datetime, timezone, tzinfo
from zoneinfo import ZoneInfo

import plotly.colors as plc
import plotly.graph_objects as go
from plotly.subplots import make_subplots

import config
from api_client import KalshiClient

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
START_DATE = "2026-3-19"      # Only include activity from this date onward (YYYY-MM-DD)
OFFSET_DOLLARS = 0            # Shift the P&L baseline (e.g. initial deposit adjustment)
PORTFOLIO_ALLOCATION = 0.02   # The % of your account allocated per trade (0.10 = 10%)
OUTPUT_FILE = "performance.html"
EVENTS_CSV_FILE = "performance_events.csv"
# Local timezone for chart times, daily P&L buckets, and hourly frequency (IANA).
# Must match your wall clock (e.g. America/Los_Angeles if times were ~7h ahead of Pacific).
HOURLY_FREQUENCY_TIMEZONE = "America/Los_Angeles"


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

def _parse_ts(start_date: str) -> int:
    """Convert YYYY-MM-DD string to Unix timestamp."""
    dt = datetime.strptime(start_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    return int(dt.timestamp())


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

    Drops events with ``cost_cents == 0``.
    """
    ticker_cost: dict[str, float] = defaultdict(float)  # total spent (cents)
    ticker_buy_count: dict[str, int] = defaultdict(int)  # total contracts bought
    ticker_sell_revenue: dict[str, float] = defaultdict(float)
    ticker_side_counts: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))

    for f in fills:
        ticker = f.get("ticker") or f.get("market_ticker") or ""
        action = f.get("action", "")
        price = _fill_price_cents(f)
        count = _fill_count(f)
        if action == "buy":
            ticker_cost[ticker] += price * count
            ticker_buy_count[ticker] += count
            fill_side = f.get("side", "no")
            ticker_side_counts[ticker][fill_side] += count
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


def write_events_csv(events: list[dict], path: str) -> None:
    """Omits rows with buy_count == 0. Adds contract_cost and percent_gain.

    Excludes columns: ts_utc, ticker, type (ts_local is kept).
    """
    display_tz, _ = _resolve_display_timezone()
    rows = [e for e in events if int(e.get("buy_count") or 0) != 0]
    derived = ("contract_cost", "percent_gain")
    _csv_skip_keys = frozenset({"ts", "ticker", "type", *derived})
    if rows:
        keys: set[str] = set()
        for e in rows:
            keys |= e.keys()
        keys -= _csv_skip_keys
        fieldnames = ["ts_local"] + sorted(keys) + list(derived)
    else:
        fieldnames = [
            "ts_local",
            "series",
            "pnl_cents",
            "cost_cents",
            "buy_count",
            "fill_side",
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
            row: dict[str, object] = {
                "ts_local": _ts_for_display(ts, display_tz).isoformat(),
            }
            cost = float(e.get("cost_cents") or 0)
            pnl = float(e.get("pnl_cents") or 0)
            bc = int(e.get("buy_count") or 0)
            for k in fieldnames[1:]:
                if k == "contract_cost":
                    row[k] = _csv_derived_contract_cost(cost, bc)
                elif k == "percent_gain":
                    row[k] = _csv_derived_percent_gain(cost, pnl)
                else:
                    row[k] = e.get(k, "")
            w.writerow(row)
    print(f"Events CSV saved to {path} ({len(rows)} rows, buy_count != 0)")


# ---------------------------------------------------------------------------
# Chart building
# ---------------------------------------------------------------------------

def build_dashboard(events: list[dict], offset_dollars: float, current_balance: float, alloc_pct: float, output_file: str) -> None:
    if not events:
        print("No data to plot.")
        sys.exit(0)

    display_tz, display_tz_label = _resolve_display_timezone()

    timestamps = []
    cumulative_pnl = []
    running = offset_dollars * 100 
    for e in events:
        running += e["pnl_cents"]
        timestamps.append(_ts_for_display(e["ts"], display_tz))
        cumulative_pnl.append(running / 100)

    daily_pnl: dict[str, float] = defaultdict(float)
    for e in events:
        day = _ts_for_display(e["ts"], display_tz).strftime("%Y-%m-%d")
        daily_pnl[day] += e["pnl_cents"] / 100
    daily_dates = sorted(daily_pnl.keys())
    daily_values = [daily_pnl[d] for d in daily_dates]
    daily_colors = ["#2ecc71" if v >= 0 else "#e74c3c" for v in daily_values]

    pct_timestamps = []
    pct_values = []
    running_pnl_for_pct = 0.0
    running_cost_for_pct = 0.0
    markets_with_cost = 0
    for e in events:
        running_pnl_for_pct += e["pnl_cents"]
        cost = e.get("cost_cents", 0)
        running_cost_for_pct += cost
        if cost > 0:
            markets_with_cost += 1
        pct_timestamps.append(_ts_for_display(e["ts"], display_tz))
        avg_cost = (running_cost_for_pct / markets_with_cost) if markets_with_cost > 0 else 0
        pct = (running_pnl_for_pct / avg_cost * 100) if avg_cost > 0 else 0.0
        pct_values.append(pct)

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
        if e["pnl_cents"] > 0:
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
        series_pnl_totals[ser] += float(e.get("pnl_cents") or 0)
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
        if e["pnl_cents"] > 0:
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
        returns = [(e["pnl_cents"] / e["cost_cents"]) * 100 for e in valid_trades]
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
            ret = (e["pnl_cents"] / e["cost_cents"]) * 100
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
    fig = make_subplots(
        rows=8,
        cols=1,
        subplot_titles=(
            "Cumulative P&L ($)",
            "Cumulative Return (%)",
            "Daily P&L ($)",
            "Win Rate by Series & side (YES / NO) (%)",
            "P&L ($) & return (%) by series",
            "Win Rate by Side (%)",
            "Trade frequency by hour",
            f"Account Value Projection (Next 30 Days at {alloc_pct*100:.0f}% Alloc)",
        ),
        vertical_spacing=0.042,
        row_heights=[0.12, 0.10, 0.10, 0.12, 0.11, 0.10, 0.12, 0.23],
        specs=[
            [{}],
            [{}],
            [{}],
            [{}],
            [{"secondary_y": True}],
            [{}],
            [{}],
            [{}],
        ],
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
    # Global Stats & Text Formatting
    # -----------------------------------------------------------------------
    total_pnl = cumulative_pnl[-1] if cumulative_pnl else 0
    trades = [e for e in events if e["type"] == "settlement" and e.get("cost_cents", 0) > 0 and e.get("pnl_cents", 0) != 0]
    total_trades = len(trades)
    total_trade_cost = sum(e["cost_cents"] for e in trades)
    avg_total_cost = (total_trade_cost / total_trades / 100) if total_trades > 0 else 0

    total_contracts = sum(e.get("buy_count", 0) for e in trades)
    avg_contract_price = (total_trade_cost / total_contracts) if total_contracts > 0 else 0
    expected_win_rate = avg_contract_price

    wins = sum(1 for e in trades if e["pnl_cents"] > 0)
    actual_win_rate = (wins / total_trades * 100) if total_trades > 0 else 0

    pnl_color = "#2ecc71" if total_pnl >= 0 else "#e74c3c"
    pct_color = "#2ecc71" if last_pct >= 0 else "#e74c3c"
    wr_color = "#2ecc71" if actual_win_rate >= expected_win_rate else "#e74c3c"

    fig.update_layout(
        title=dict(
            text=(
                f"Kalshi Bot Performance | Account Value: ${current_balance:,.2f} | Portfolio Alloc: {alloc_pct*100:.0f}%<br>"
                f"P&L: <span style='color:{pnl_color}'>${total_pnl:+,.2f}</span> | "
                f"Return: <span style='color:{pct_color}'>{last_pct:+.2f}%</span> | "
                f"Avg Cost: ${avg_total_cost:,.2f} | "
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
                f"</span><br><br>"
            ),
            font=dict(size=18),
            y=0.98,
        ),
        height=2880,
        margin=dict(t=160, b=155),
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
                f"Trades: {total_trades}\n"
                f"Expected Win Rate: {expected_win_rate:.1f}% | "
                f"Actual Win Rate: {actual_win_rate:.1f}%"
                f" ({wins}/{total_trades})\n"
                f"Sum Trade Return: {total_trade_return_pct:+.2f}% | "
                f"Avg Trade Return: {avg_trade_return_pct:+.2f}% | "
                f"Hourly Gain: {hourly_gain:+.4f}% | "
                f"Daily Gain: {daily_gain:+.2f}% | "
                f"Weekly Gain: {weekly_gain:+.2f}%"
            )
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

    write_events_csv(events, EVENTS_CSV_FILE)
    build_dashboard(events, OFFSET_DOLLARS, current_balance, PORTFOLIO_ALLOCATION, OUTPUT_FILE)


if __name__ == "__main__":
    main()