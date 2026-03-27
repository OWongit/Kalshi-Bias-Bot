"""
Plot entry price (cents) vs aggregate trade win rate (%) for all markets in a
data directory. For each entry price, runs the same backtest as ``backtest.py``,
then draws the win rate with 95%% Wilson (binomial) confidence bands
(smoothed along entry with a centered moving average) and a reference line
y = x (win rate %% vs entry %% on the same 0–100 scale).

Usage:
    python calibration/plot_entry_vs_winrate.py
    python calibration/plot_entry_vs_winrate.py calibration/past_data/KXBTC15M
    python calibration/plot_entry_vs_winrate.py --entry-min 85 --entry-max 99 \\
        calibration/past_data/KXETH15M --output my_plot.html

Set DATA_DIR below to run without passing a path on the command line.
"""

from __future__ import annotations

import argparse
import math
import os
import sys

_cal_dir = os.path.dirname(os.path.abspath(__file__))
if _cal_dir not in sys.path:
    sys.path.insert(0, _cal_dir)

import plotly.graph_objects as go

from backtest import run_backtest

# Defaults (override via CLI)
DATA_DIR = "calibration/past_data/KXSOL15M"  # directory with _markets.csv + per-ticker CSVs
ENTRY_MIN = 90
ENTRY_MAX = 99
STOP_LOSS = 0
MAX_SPREAD = 1
MIN_OPEN_INTEREST = None
COOLDOWN_SECONDS = 0
SIDE = "no"
LOOKBACK_DAYS = None
# Centered moving-average window for CI upper/lower along entry (odd ≥3, or 1 = raw Wilson)
CI_MA_WINDOW = 1
OUTPUT_FILE = "calibration/entry_vs_winrate.html"


def wilson_ci(wins: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """95% Wilson score interval for binomial proportion; returns (low, high) in [0, 1].

    Uses the standard formula so the sample proportion *wins/n* always lies inside
    (low, high) when 0 < wins < n (given floating-point rounding).
    """
    if n <= 0:
        return (0.0, 0.0)
    p = wins / n
    z2 = z * z
    denom = 1.0 + z2 / n
    inner = p * (1.0 - p) / n + z2 / (4.0 * n * n)
    s = math.sqrt(max(0.0, inner))
    mid = p + z2 / (2.0 * n)
    lower = (mid - z * s) / denom
    upper = (mid + z * s) / denom
    return (max(0.0, lower), min(1.0, upper))


def _centered_moving_average(values: list[float], window: int) -> list[float]:
    """Centered MA; at edges uses all available points within half-window. window 1 = copy."""
    if window <= 1 or len(values) <= 1:
        return list(values)
    w = window if window % 2 == 1 else window + 1
    half = w // 2
    n = len(values)
    out: list[float] = []
    for i in range(n):
        a = max(0, i - half)
        b = min(n, i + half + 1)
        chunk = values[a:b]
        out.append(sum(chunk) / len(chunk))
    return out


def smooth_ci_bands(
    lo: list[float], hi: list[float], window: int
) -> tuple[list[float], list[float]]:
    """Apply the same centered MA to CI bounds; enforce lo ≤ hi and [0, 100]."""
    lo_s = _centered_moving_average(lo, window)
    hi_s = _centered_moving_average(hi, window)
    fixed_lo: list[float] = []
    fixed_hi: list[float] = []
    for a, b in zip(lo_s, hi_s):
        low, high = (a, b) if a <= b else (b, a)
        fixed_lo.append(max(0.0, min(100.0, low)))
        fixed_hi.append(max(0.0, min(100.0, high)))
    return fixed_lo, fixed_hi


def collect_curve(
    data_dir: str,
    entry_min: int,
    entry_max: int,
    stop_loss: int,
    max_spread: int,
    min_open_interest: int | None,
    cooldown_seconds: int,
    side: str,
    lookback_days: int | None,
) -> tuple[list[int], list[float], list[float], list[float], list[int]]:
    """Return (entry_prices, win_rates_pct, ci_low_pct, ci_high_pct, n_trades per point)."""
    xs: list[int] = []
    ys: list[float] = []
    lo: list[float] = []
    hi: list[float] = []
    ns: list[int] = []

    for ep in range(entry_min, entry_max + 1):
        if stop_loss and ep <= stop_loss:
            continue
        summary = run_backtest(
            data_dir,
            ep,
            stop_loss,
            max_spread,
            min_open_interest,
            cooldown_seconds,
            verbose=False,
            ticker_filter=None,
            side=side,
            lookback_days=lookback_days,
        )
        w = int(summary["wins"])
        n = int(summary["total_trades"])
        if n <= 0:
            continue
        p = w / n
        ci_l, ci_h = wilson_ci(w, n)
        xs.append(ep)
        ys.append(100.0 * p)
        lo.append(100.0 * ci_l)
        hi.append(100.0 * ci_h)
        ns.append(n)

    return xs, ys, lo, hi, ns


def build_figure(
    data_dir: str,
    entry_min: int,
    entry_max: int,
    xs: list[int],
    ys: list[float],
    lo: list[float],
    hi: list[float],
    ns: list[int],
    ci_ma_window: int,
) -> go.Figure:
    slug = os.path.basename(os.path.normpath(data_dir))
    axis_max = 100.0
    fig = go.Figure()

    # 95% Wilson CI band (upper then lower with fill to next)
    fig.add_trace(
        go.Scatter(
            x=xs,
            y=hi,
            mode="lines",
            line=dict(color="#3498db", width=1, dash="dash"),
            name="CI upper",
            legendgroup="ci",
            showlegend=False,
            hovertemplate="entry=%{x}¢<br>CI high=%{y:.1f}%<extra></extra>",
        )
    )
    fig.add_trace(
        go.Scatter(
            x=xs,
            y=lo,
            mode="lines",
            line=dict(color="#3498db", width=1, dash="dash"),
            fill="tonexty",
            fillcolor="rgba(52, 152, 219, 0.25)",
            name=(
                "95% CI (Wilson)"
                if ci_ma_window <= 1
                else f"95% CI (Wilson, {ci_ma_window}-pt MA)"
            ),
            legendgroup="ci",
            hovertemplate="entry=%{x}¢<br>CI low=%{y:.1f}%<extra></extra>",
        )
    )
    fig.add_trace(
        go.Scatter(
            x=xs,
            y=ys,
            mode="lines+markers",
            line=dict(color="#e74c3c", width=2),
            marker=dict(size=8),
            name="Win rate",
            customdata=list(zip(ns)),
            hovertemplate=(
                "entry=%{x}¢<br>win rate=%{y:.1f}%<br>"
                "trades=%{customdata[0]}<extra></extra>"
            ),
        )
    )

    # Reference y = x (same units: cents as % on both axes)
    fig.add_trace(
        go.Scatter(
            x=[0, axis_max],
            y=[0, axis_max],
            mode="lines",
            line=dict(color="#95a5a6", width=2, dash="dot"),
            name="y = x",
            hovertemplate="y = x reference<extra></extra>",
        )
    )

    fig.update_layout(
        title=dict(
            text=(
                f"Entry price vs win rate — {slug}<br>"
                f"<span style=\"font-size:12px;color:#aaa\">"
                f"{data_dir} · entry {entry_min}–{entry_max}¢ · n points={len(xs)}"
                + (
                    ""
                    if ci_ma_window <= 1
                    else f" · CI band = {ci_ma_window}-pt centered MA"
                )
                + "</span>"
            ),
        ),
        xaxis=dict(
            title="Entry (ask) price (¢)",
            range=[0, axis_max],
            zeroline=True,
        ),
        yaxis=dict(
            title="Win rate (%)",
            range=[0, axis_max],
            zeroline=True,
        ),
        template="plotly_dark",
        paper_bgcolor="#1a1a2e",
        plot_bgcolor="#16213e",
        font=dict(color="#e0e0e0"),
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
        margin=dict(t=100, l=60, r=40, b=60),
        height=640,
        width=900,
    )
    return fig


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Plot entry price vs backtest win rate with Wilson 95% CI and y=x line.",
    )
    parser.add_argument(
        "data_dir",
        nargs="?",
        default=None,
        help="Directory with _markets.csv + CSVs (default: DATA_DIR in this file)",
    )
    parser.add_argument("--entry-min", type=int, default=ENTRY_MIN)
    parser.add_argument("--entry-max", type=int, default=ENTRY_MAX)
    parser.add_argument("--stop-loss", type=int, default=STOP_LOSS)
    parser.add_argument("--max-spread", type=int, default=MAX_SPREAD)
    parser.add_argument(
        "--min-open-interest",
        type=int,
        default=None,
        help="Omit for no filter (default)",
    )
    parser.add_argument("--cooldown", type=int, default=COOLDOWN_SECONDS)
    parser.add_argument("--side", choices=("yes", "no", "both"), default=SIDE)
    parser.add_argument(
        "--lookback-days",
        type=int,
        default=None,
        help="Same as backtest LOOKBACK_DAYS; omit for all data",
    )
    parser.add_argument("-o", "--output", default=OUTPUT_FILE, help="Output HTML path")
    parser.add_argument(
        "--ci-ma-window",
        type=int,
        default=CI_MA_WINDOW,
        metavar="N",
        help="Centered moving-average length for CI bounds along entry (1 = no smoothing)",
    )
    args = parser.parse_args()

    data_dir = args.data_dir or DATA_DIR
    if not data_dir:
        print("error: set DATA_DIR in this file or pass a directory argument", file=sys.stderr)
        sys.exit(1)
    if not os.path.isdir(data_dir):
        print(f"error: not a directory: {data_dir}", file=sys.stderr)
        sys.exit(1)

    min_oi = args.min_open_interest
    lookback = args.lookback_days

    print(f"Building curve: {data_dir}")
    ci_ma = max(1, args.ci_ma_window)
    print(
        f"  entry {args.entry_min}–{args.entry_max}¢  stop={args.stop_loss}¢  "
        f"max_spread={args.max_spread}  side={args.side}  lookback={lookback}  "
        f"CI MA window={ci_ma}"
    )

    xs, ys, lo, hi, ns = collect_curve(
        data_dir,
        args.entry_min,
        args.entry_max,
        args.stop_loss,
        args.max_spread,
        min_oi,
        args.cooldown,
        args.side,
        lookback,
    )

    if not xs:
        print("error: no data points (no trades at any entry in range?)", file=sys.stderr)
        sys.exit(1)

    lo_plot, hi_plot = smooth_ci_bands(lo, hi, ci_ma)

    fig = build_figure(
        data_dir,
        args.entry_min,
        args.entry_max,
        xs,
        ys,
        lo_plot,
        hi_plot,
        ns,
        ci_ma_window=ci_ma,
    )
    out = args.output
    os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
    fig.write_html(out, include_plotlyjs="cdn")
    print(f"Wrote {out}")


if __name__ == "__main__":
    main()
