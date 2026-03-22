"""
Kalshi Performance Dashboard

Fetches fills and settlements from the Kalshi API, computes cumulative P&L,
and generates an interactive HTML dashboard.

Usage:
    Edit the configuration section below, then run:
    python performance.py
"""

import sys
import time
from collections import defaultdict
from datetime import datetime, timezone

import plotly.graph_objects as go
from plotly.subplots import make_subplots

import config
from api_client import KalshiClient

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
START_DATE = "2026-3-21"     # Only include activity from this date onward (YYYY-MM-DD)
OFFSET_DOLLARS = 0            # Shift the P&L baseline (e.g. initial deposit adjustment)
OUTPUT_FILE = "performance.html"


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

    For settlements: P&L = settlement revenue - total cost of contracts bought.
    For sells (before settlement): P&L = sell revenue - proportional buy cost.
    """
    # Accumulate net cost and contract counts per ticker from fills
    ticker_cost: dict[str, float] = defaultdict(float)  # total spent (cents)
    ticker_buy_count: dict[str, int] = defaultdict(int)  # total contracts bought
    ticker_sell_revenue: dict[str, float] = defaultdict(float)

    for f in fills:
        ticker = f.get("ticker") or f.get("market_ticker") or ""
        action = f.get("action", "")
        price = _fill_price_cents(f)
        count = _fill_count(f)
        if action == "buy":
            ticker_cost[ticker] += price * count
            ticker_buy_count[ticker] += count
        elif action == "sell":
            ticker_sell_revenue[ticker] += price * count

    events: list[dict] = []

    # Settlement events: realized P&L = revenue + sell proceeds - buy cost
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

        events.append({
            "ts": ts,
            "ticker": ticker,
            "series": _extract_series(ticker),
            "type": "settlement",
            "pnl_cents": pnl,
            "cost_cents": cost,
            "buy_count": ticker_buy_count.get(ticker, 0),
        })

    # For tickers with sells but no settlement yet, show realized sell P&L
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
        events.append({
            "ts": ts,
            "ticker": ticker,
            "series": _extract_series(ticker),
            "type": "sell",
            "pnl_cents": price * count,
            "cost_cents": sell_cost,
            "buy_count": ticker_buy_count.get(ticker, 0),
        })

    events.sort(key=lambda e: e["ts"])
    return events


# ---------------------------------------------------------------------------
# Chart building
# ---------------------------------------------------------------------------

def build_dashboard(events: list[dict], offset_dollars: float, output_file: str) -> None:
    if not events:
        print("No data to plot.")
        sys.exit(0)

    # Cumulative P&L timeline (only realized profit/loss)
    timestamps = []
    cumulative_pnl = []
    running = offset_dollars * 100  # cents
    for e in events:
        running += e["pnl_cents"]
        timestamps.append(e["ts"])
        cumulative_pnl.append(running / 100)

    # Daily P&L
    daily_pnl: dict[str, float] = defaultdict(float)
    for e in events:
        day = e["ts"].strftime("%Y-%m-%d")
        daily_pnl[day] += e["pnl_cents"] / 100
    daily_dates = sorted(daily_pnl.keys())
    daily_values = [daily_pnl[d] for d in daily_dates]
    daily_colors = ["#2ecc71" if v >= 0 else "#e74c3c" for v in daily_values]

    # Cumulative percent return: total P&L / avg total cost per market
    # avg total cost = total capital spent / number of markets with cost > 0
    # Result is a ratio displayed as %, e.g. 5.78 / 3.28 = 176%
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
        pct_timestamps.append(e["ts"])
        avg_cost = (running_cost_for_pct / markets_with_cost) if markets_with_cost > 0 else 0
        pct = (running_pnl_for_pct / avg_cost * 100) if avg_cost > 0 else 0.0
        pct_values.append(pct)

    # P&L by series
    series_pnl: dict[str, float] = defaultdict(float)
    for e in events:
        series_pnl[e["series"]] += e["pnl_cents"] / 100
    sorted_series = sorted(series_pnl.items(), key=lambda x: x[1], reverse=True)
    series_names = [s[0] for s in sorted_series]
    series_values = [s[1] for s in sorted_series]
    series_colors = ["#2ecc71" if v >= 0 else "#e74c3c" for v in series_values]

    fig = make_subplots(
        rows=4, cols=1,
        subplot_titles=(
            "Cumulative P&L ($)",
            "Cumulative Return (%)",
            "Daily P&L ($)",
            "P&L by Series",
        ),
        vertical_spacing=0.06,
        row_heights=[0.28, 0.22, 0.22, 0.28],
    )

    fig.add_trace(
        go.Scatter(
            x=timestamps, y=cumulative_pnl,
            mode="lines",
            line=dict(color="#3498db", width=2),
            fill="tozeroy",
            fillcolor="rgba(52, 152, 219, 0.15)",
            name="Cumulative P&L",
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
        ),
        row=2, col=1,
    )
    fig.add_hline(y=0, line_dash="dash", line_color="gray", opacity=0.5, row=2, col=1)

    fig.add_trace(
        go.Bar(
            x=daily_dates, y=daily_values,
            marker_color=daily_colors,
            name="Daily P&L",
        ),
        row=3, col=1,
    )
    fig.add_hline(y=0, line_dash="dash", line_color="gray", opacity=0.5, row=3, col=1)

    fig.add_trace(
        go.Bar(
            y=series_names, x=series_values,
            orientation="h",
            marker_color=series_colors,
            name="Series P&L",
        ),
        row=4, col=1,
    )
    fig.add_vline(x=0, line_dash="dash", line_color="gray", opacity=0.5, row=4, col=1)

    total_pnl = cumulative_pnl[-1] if cumulative_pnl else 0
    settled_with_cost = [e for e in events if e["type"] == "settlement" and e.get("cost_cents", 0) > 0]
    total_settlements = sum(1 for e in events if e["type"] == "settlement")
    total_settled_cost = sum(e["cost_cents"] for e in settled_with_cost)
    avg_total_cost = (total_settled_cost / len(settled_with_cost) / 100) if settled_with_cost else 0
    pnl_color = "#2ecc71" if total_pnl >= 0 else "#e74c3c"
    pct_color = "#2ecc71" if last_pct >= 0 else "#e74c3c"

    fig.update_layout(
        title=dict(
            text=(
                f"Kalshi Bot Performance | "
                f"P&L: <span style='color:{pnl_color}'>${total_pnl:+,.2f}</span> | "
                f"Return: <span style='color:{pct_color}'>{last_pct:+.2f}%</span> | "
                f"Avg Cost: ${avg_total_cost:,.2f} | "
                f"Settlements: {total_settlements}"
            ),
            font=dict(size=18),
        ),
        height=1500,
        showlegend=False,
        template="plotly_dark",
        paper_bgcolor="#1a1a2e",
        plot_bgcolor="#16213e",
        font=dict(color="#e0e0e0"),
    )

    fig.update_yaxes(title_text="P&L ($)", row=1, col=1)
    fig.update_yaxes(title_text="Return (%)", row=2, col=1)
    fig.update_yaxes(title_text="P&L ($)", row=3, col=1)
    fig.update_xaxes(title_text="P&L ($)", row=4, col=1)
    fig.update_yaxes(autorange="reversed", row=4, col=1)

    fig.write_html(output_file, include_plotlyjs="cdn")
    print(f"Dashboard saved to {output_file}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    private_key = config.load_private_key()
    client = KalshiClient(config.BASE_URL, config.API_KEY_ID, private_key)

    min_ts = _parse_ts(START_DATE)

    print(f"Fetching fills since {START_DATE} ...")
    fills = fetch_all_fills(client, min_ts)
    print(f"  {len(fills)} fills")

    print(f"Fetching settlements since {START_DATE} ...")
    settlements = fetch_all_settlements(client, min_ts)
    print(f"  {len(settlements)} settlements")

    events = build_pnl_events(fills, settlements)
    print(f"  {len(events)} total P&L events")

    build_dashboard(events, OFFSET_DOLLARS, OUTPUT_FILE)


if __name__ == "__main__":
    main()
