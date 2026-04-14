"""
Bid-based backtest variant.

Supports decimal entry prices (e.g. 97.5, 98.1).

Usage:
    python calibration/backtest_temp.py

    To swap into the optimizer, change the import in optimize.py:
        from backtest_temp import load_markets_manifest, parse_event_date_from_ticker, run_backtest

No imports from the parent project — reads only CSV files.
"""

import csv
import math
import os
import re
import statistics
import sys
from datetime import date, timedelta


def _safe_int(val):
    """Parse a CSV cell to int, returning None for empty / invalid values."""
    if val is None or val == "":
        return None
    try:
        return int(round(float(val)))
    except (ValueError, TypeError):
        return None


_MONTH_ABBREV = {
    "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
    "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12,
}

_TICKER_DATE_RE = re.compile(
    r"-(\d{2})(JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)(\d{2})",
    re.IGNORECASE,
)


def parse_event_date_from_ticker(ticker: str) -> date | None:
    """Return the event calendar date encoded in *ticker*, or None if not found."""
    m = _TICKER_DATE_RE.search(ticker)
    if not m:
        return None
    yy = int(m.group(1))
    mon = m.group(2).upper()
    dd = int(m.group(3))
    month = _MONTH_ABBREV.get(mon)
    if month is None:
        return None
    year = 2000 + yy if yy < 100 else yy
    try:
        return date(year, month, dd)
    except ValueError:
        return None


def load_markets_manifest(data_dir: str) -> dict[str, dict]:
    """Read _markets.csv and return {ticker: {result, open_time, close_time}}."""
    path = os.path.join(data_dir, "_markets.csv")
    manifest: dict[str, dict] = {}
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            manifest[row["ticker"]] = row
    return manifest


_CANDLE_STRING_KEYS = frozenset({"price_level_structure"})


def load_candles(csv_path: str) -> list[dict]:
    """Read a market candlestick CSV into a list of dicts.

    Numeric columns use ``_safe_int``. String-typed columns
    (``price_level_structure``, ``*_dollars``) are kept as-is.
    """
    candles: list[dict] = []
    with open(csv_path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            rec: dict = {}
            for k, v in row.items():
                if k in _CANDLE_STRING_KEYS or k.endswith("_dollars"):
                    s = (v or "").strip()
                    rec[k] = s if s else None
                else:
                    rec[k] = _safe_int(v)
            candles.append(rec)
    return candles[: len(candles) - 1]


def _side_bid_high(candle: dict, side: str) -> int | float | None:
    """Highest bid for *side* within the candle (cents), from CSV OHLC.

    Candles store YES bid/ask OHLC. For NO, mirror the close-path mapping
    ``no_bid ≈ 100 - yes_ask``: the **high** NO bid in the bar is
    ``100 - yes_ask_low`` (same convention as ``bid_low = 100 - yes_ask_high``
    for stop-loss).
    """
    if side == "yes":
        return candle.get("yes_bid_high")
    yal = candle.get("yes_ask_low")
    yac = candle.get("yes_ask_close")
    if yal is not None:
        return 100 - yal
    if yac is not None:
        return 100 - yac
    return None


def _try_entry(
    candle: dict,
    entry_prices: list[float | int],
    max_spread: int,
    min_open_interest: int | None,
    side: str,
    buy_if_bid_gt_entry: bool = False,
    bid_limit_offset: float = 0.0,
) -> float | int | None:
    """Return the simulated entry cost in cents, or None if no entry.

    Entry uses the **intrabar bid high** (``yes_bid_high`` for YES;
    ``100 - yes_ask_low`` for NO). Spread and sanity checks still use close.

    With ``buy_if_bid_gt_entry=True``: enter when ``bid_high >= ep``.
    With ``buy_if_bid_gt_entry=False``: enter when ``bid_high == ep`` (float-tolerant).
    Fill price is ``bid_high + bid_limit_offset``, clamped to [1, 99].
    """
    if side == "no":
        yes_bid_close = candle.get("yes_bid_close")
        yes_ask_close = candle.get("yes_ask_close")
        if yes_bid_close is None or yes_ask_close is None:
            return None
        ask_close = 100 - yes_bid_close
        bid_close = 100 - yes_ask_close
    else:
        ask_close = candle.get("yes_ask_close")
        bid_close = candle.get("yes_bid_close")
        if ask_close is None or bid_close is None:
            return None

    if ask_close - bid_close > max_spread:
        return None
    if bid_close < 1 or bid_close >= 100:
        return None
    if min_open_interest is not None:
        oi = candle.get("open_interest") or 0
        if oi < min_open_interest:
            return None

    bid_high = _side_bid_high(candle, side)
    if bid_high is None:
        return None

    for ep in entry_prices:
        ep_f = float(ep)
        ok = (
            (buy_if_bid_gt_entry and float(bid_high) >= ep_f)
            or (
                not buy_if_bid_gt_entry
                and math.isclose(float(bid_high), ep_f, rel_tol=0, abs_tol=1e-6)
            )
        )
        if ok:
            fill = float(bid_high) + bid_limit_offset
            return max(1.0, min(99.0, fill))

    return None


def simulate_market(
    candles: list[dict],
    result: str,
    entry_price: float | int | list[float | int],
    stop_loss: int,
    max_spread: int,
    min_open_interest: int | None = None,
    cooldown_seconds: int = 0,
    side: str = "no",
    buy_if_bid_gt_entry: bool = False,
    bid_limit_offset: float = 0.0,
) -> list[dict]:
    """Simulate the bid-based entry/stop-loss strategy on one market's candle series.

    Args:
        entry_price: Bid price(s) in cents — supports decimals (e.g. 97.5).
            Pass a single number or a list.
        side: "yes", "no", or "both".
        buy_if_bid_gt_entry: True = enter when bid_high >= entry; False = bid_high == entry only.
        bid_limit_offset: Added to bid_high for simulated fill price (cents).

    Returns a list of trade dicts, each with:
        entry_cost, exit_price, pnl, exit_reason, side
    """
    trades: list[dict] = []
    holding = False
    entry_cost: float | int = 0
    entry_side = ""
    cooldown_until = 0

    entry_prices: list[float | int] = (
        list(entry_price) if isinstance(entry_price, list) else [entry_price]
    )

    sides_to_check = ["no", "yes"] if side == "both" else [side]

    for candle in candles:
        ts = candle.get("end_period_ts") or 0

        yes_bid_close = candle.get("yes_bid_close")
        yes_ask_close = candle.get("yes_ask_close")
        if yes_bid_close is None or yes_ask_close is None:
            continue

        if holding:
            if entry_side == "no":
                yes_ask_high = candle.get("yes_ask_high")
                bid_low = (100 - yes_ask_high) if yes_ask_high is not None else (100 - yes_ask_close)
            else:
                yes_bid_low = candle.get("yes_bid_low")
                bid_low = yes_bid_low if yes_bid_low is not None else yes_bid_close

            if stop_loss and bid_low <= stop_loss:
                pnl = float(stop_loss) - float(entry_cost)
                trades.append({
                    "entry_cost": entry_cost,
                    "exit_price": stop_loss,
                    "pnl": pnl,
                    "exit_reason": "stop_loss",
                    "side": entry_side,
                })
                holding = False
                cooldown_until = ts + cooldown_seconds
                continue
        else:
            if ts < cooldown_until:
                continue
            for s in sides_to_check:
                cost = _try_entry(
                    candle, entry_prices, max_spread, min_open_interest, s,
                    buy_if_bid_gt_entry, bid_limit_offset,
                )
                if cost is not None:
                    entry_cost = cost
                    entry_side = s
                    holding = True
                    break

    if holding:
        won = (result == entry_side)
        ec = float(entry_cost)
        pnl = (100.0 - ec) if won else (0.0 - ec)
        trades.append({
            "entry_cost": entry_cost,
            "exit_price": 100 if won else 0,
            "pnl": pnl,
            "exit_reason": "settlement_win" if won else "settlement_loss",
            "side": entry_side,
        })

    return trades


def _compute_composite_stats(market_stats: list[dict]) -> dict:
    """Compute composite score statistics from per-market pnl/win stats."""
    traded = [m for m in market_stats if m["cost"] > 0]
    if not traded:
        return {
            "pct_return": 0.0,
            "pct_return_ci_95": (0.0, 0.0),
            "win_rate": 0.0,
            "median_return": 0.0,
            "pct_profitable_markets": 0.0,
            "sharpe_like": 0.0,
            "t_stat": 0.0,
            "composite_score": 0.0,
        }

    total_pnl = sum(m["pnl"] for m in market_stats)
    total_cost = sum(m["cost"] for m in market_stats)
    total_trades = sum(m["trades"] for m in market_stats)
    total_wins = sum(m["wins"] for m in market_stats)

    pct_return = (total_pnl / total_cost * 100) if total_cost else 0.0
    win_rate = (total_wins / total_trades * 100) if total_trades else 0.0

    per_market_returns = [
        (m["pnl"] / m["cost"] * 100) for m in traded
    ]
    median_return = statistics.median(per_market_returns)
    n = len(traded)
    mean_return = statistics.mean(per_market_returns)
    std_return = statistics.stdev(per_market_returns) if n > 1 else 0.0

    pct_profitable_markets = (
        sum(1 for m in traded if m["pnl"] > 0) / n * 100
    )

    sharpe_like = (
        mean_return / std_return
        if std_return > 0 else (mean_return if mean_return > 0 else 0.0)
    )
    t_stat = (
        mean_return / (std_return / math.sqrt(n))
        if std_return > 0 else (float("inf") if mean_return > 0 else 0.0)
    )

    t_critical = 1.96
    se = (std_return / math.sqrt(n)) if n > 0 else 0.0
    ci_low = mean_return - t_critical * se
    ci_high = mean_return + t_critical * se
    pct_return_ci_95 = (ci_low, ci_high)

    composite_score = pct_return * abs(t_stat)

    return {
        "pct_return": pct_return,
        "pct_return_ci_95": pct_return_ci_95,
        "win_rate": win_rate,
        "median_return": median_return,
        "pct_profitable_markets": pct_profitable_markets,
        "sharpe_like": sharpe_like,
        "t_stat": t_stat,
        "composite_score": composite_score,
    }


def run_backtest(
    data_dir: str,
    entry_price: float | int | list[float | int],
    stop_loss: int,
    max_spread: int,
    min_open_interest: int | None = None,
    cooldown_seconds: int = 0,
    verbose: bool = False,
    ticker_filter: set[str] | None = None,
    side: str = "no",
    lookback_days: int | None = None,
    as_of: date | None = None,
    buy_if_bid_gt_entry: bool = False,
    bid_limit_offset: float = 0.0,
) -> dict:
    """Run the bid-based backtest over all markets in *data_dir*.

    ``entry_price`` may be a single number or a list (supports decimals like
    97.5).
    """
    manifest = load_markets_manifest(data_dir)
    ref = as_of or date.today()
    if lookback_days is not None and lookback_days > 0:
        lookback_cutoff = ref - timedelta(days=max(0, lookback_days - 1))
    else:
        lookback_cutoff = None

    market_stats: list[dict] = []

    for ticker, meta in manifest.items():
        if ticker_filter is not None and ticker not in ticker_filter:
            continue
        if lookback_cutoff is not None:
            ev = parse_event_date_from_ticker(ticker)
            if ev is None or ev < lookback_cutoff:
                continue
        csv_path = os.path.join(data_dir, f"{ticker}.csv")
        if not os.path.exists(csv_path):
            continue

        candles = load_candles(csv_path)
        if not candles:
            continue

        trades = simulate_market(
            candles, meta.get("result", ""), entry_price, stop_loss, max_spread,
            min_open_interest, cooldown_seconds, side=side,
            buy_if_bid_gt_entry=buy_if_bid_gt_entry,
            bid_limit_offset=bid_limit_offset,
        )

        mkt_pnl = sum(t["pnl"] for t in trades)
        mkt_cost = sum(t["entry_cost"] for t in trades)
        mkt_wins = sum(1 for t in trades if t["pnl"] > 0)
        mkt_losses = sum(1 for t in trades if t["pnl"] <= 0)

        if verbose and trades:
            pct = (mkt_pnl / mkt_cost * 100) if mkt_cost else 0.0
            print(
                f"  {ticker:40s}  trades={len(trades):3d}  "
                f"pnl={mkt_pnl:+8.2f}¢  cost={mkt_cost:8.2f}¢  "
                f"return={pct:+.1f}%  W/L={mkt_wins}/{mkt_losses}"
            )

        market_stats.append({
            "ticker": ticker,
            "pnl": mkt_pnl,
            "cost": mkt_cost,
            "trades": len(trades),
            "wins": mkt_wins,
            "losses": mkt_losses,
        })

    composite = _compute_composite_stats(market_stats)

    total_trades = sum(m["trades"] for m in market_stats)
    total_pnl = sum(m["pnl"] for m in market_stats)
    total_cost = sum(m["cost"] for m in market_stats)
    wins = sum(m["wins"] for m in market_stats)
    losses = sum(m["losses"] for m in market_stats)

    summary = {
        "entry_price": entry_price,
        "stop_loss": stop_loss,
        "max_spread": max_spread,
        "total_trades": total_trades,
        "total_pnl": total_pnl,
        "total_cost": total_cost,
        "wins": wins,
        "losses": losses,
        "market_stats": market_stats,
        "market_results": [
            {"ticker": m["ticker"], "trades": m["trades"], "pnl": m["pnl"], "cost": m["cost"]}
            for m in market_stats
        ],
        **composite,
    }
    return summary


def run_backtest_single(
    csv_path: str,
    result: str,
    entry_price: float | int | list[float | int],
    stop_loss: int,
    max_spread: int,
    min_open_interest: int | None = None,
    cooldown_seconds: int = 0,
    verbose: bool = False,
    side: str = "no",
    buy_if_bid_gt_entry: bool = False,
    bid_limit_offset: float = 0.0,
) -> dict:
    """Run the bid-based backtest on a single market CSV file.

    Args:
        csv_path:    Path to a market candlestick CSV.
        result:      Settlement result for this market ("yes" or "no").
        entry_price: Bid price(s) in cents — supports decimals.
        stop_loss:   Sell when bid drops to this (cents).
        max_spread:  Max bid-ask spread for entry (cents).
        min_open_interest: Skip candles with open_interest below this (None = no filter).
        cooldown_seconds:  Seconds to wait after a stop-loss before re-entering.
        verbose:     Print per-trade details.
        side:        "yes", "no", or "both".
    """
    candles = load_candles(csv_path)
    ticker = os.path.splitext(os.path.basename(csv_path))[0]

    trades = simulate_market(candles, result, entry_price, stop_loss, max_spread,
                             min_open_interest, cooldown_seconds, side=side,
                             buy_if_bid_gt_entry=buy_if_bid_gt_entry,
                             bid_limit_offset=bid_limit_offset)

    total_pnl = sum(t["pnl"] for t in trades)
    total_cost = sum(t["entry_cost"] for t in trades)
    wins = sum(1 for t in trades if t["pnl"] > 0)
    losses = sum(1 for t in trades if t["pnl"] <= 0)
    pct_return = (total_pnl / total_cost * 100) if total_cost else 0.0
    win_rate = (wins / len(trades) * 100) if trades else 0.0

    if verbose:
        print(f"\nBacktest (single, bid-based): {ticker}  side={side}")
        print(f"  Result: {result}  |  entry_prices={entry_price!r}¢  "
              f"stop_loss={stop_loss}¢  max_spread={max_spread}¢\n")
        for j, t in enumerate(trades, 1):
            t_side = t.get('side', side)
            print(f"  Trade {j}: {t_side.upper()} bid @ {t['entry_cost']}¢  "
                  f"exit @ {t['exit_price']}¢  pnl={t['pnl']:+.2f}¢  "
                  f"({t['exit_reason']})")
        print(f"\n  Total trades:   {len(trades)}")
        print(f"  Total P/L:      {total_pnl:+.2f}¢ (${total_pnl/100:+.2f})")
        print(f"  Total cost:     {total_cost:.2f}¢ (${total_cost/100:.2f})")
        print(f"  Percent return: {pct_return:+.2f}%")
        print(f"  Wins / Losses:  {wins} / {losses}")
        print(f"  Win rate:       {win_rate:.1f}%\n")

    return {
        "ticker": ticker,
        "entry_price": entry_price,
        "stop_loss": stop_loss,
        "max_spread": max_spread,
        "total_trades": len(trades),
        "total_pnl": total_pnl,
        "total_cost": total_cost,
        "pct_return": pct_return,
        "wins": wins,
        "losses": losses,
        "win_rate": win_rate,
        "trades": trades,
    }


# ---------------------------------------------------------------------------
# Configuration — edit these values directly instead of using CLI args
# ---------------------------------------------------------------------------
DATA_DIR = "calibration/past_data/KXETH15M"
ENTRY_PRICE = 95        # Bid price to place (cents, supports decimals like 97.5)
STOP_LOSS = 0           # Sell when bid drops to this (cents, 0 = disabled)
MAX_SPREAD = 1          # Max bid-ask spread for entry (cents)
MIN_OPEN_INTEREST = None  # Skip candles with open_interest below this (None = no filter)
COOLDOWN_SECONDS = 300  # Seconds to wait after a stop-loss before re-entering
SIDE = "no"             # "yes", "no", or "both"
BUY_IF_BID_GT_ENTRY = False  # True: bid >= entry_price; False: bid == entry_price only
BID_LIMIT_OFFSET = 0.0      # Added to best bid for simulated fill price (cents)
LOOKBACK_DAYS = None    # e.g. 5, 7, or 50

SINGLE_CSV = None
SINGLE_RESULT = "no"


def main():
    entry_price = ENTRY_PRICE
    stop_loss = STOP_LOSS
    max_spread = MAX_SPREAD
    min_oi = MIN_OPEN_INTEREST
    cooldown = COOLDOWN_SECONDS
    side = SIDE
    buy_gt = BUY_IF_BID_GT_ENTRY
    offset = BID_LIMIT_OFFSET
    lookback_days = LOOKBACK_DAYS

    if SINGLE_CSV:
        run_backtest_single(
            SINGLE_CSV, SINGLE_RESULT, entry_price, stop_loss, max_spread,
            min_oi, cooldown, verbose=True, side=side,
            buy_if_bid_gt_entry=buy_gt, bid_limit_offset=offset,
        )
        return

    data_dir = DATA_DIR
    print(f"\nBacktest (bid-based): side={side}  entry_prices={entry_price!r}¢  "
          f"stop_loss={stop_loss}¢  max_spread={max_spread}¢  "
          f"min_open_interest={min_oi}  cooldown={cooldown}s  "
          f"buy_gt={buy_gt}  offset={offset}")
    print(f"Data dir: {data_dir}")
    if lookback_days is not None and lookback_days > 0:
        ref = date.today()
        lo = ref - timedelta(days=max(0, lookback_days - 1))
        print(f"Lookback: last {lookback_days} calendar day(s), event dates in [{lo}, {ref}]")
    print()

    summary = run_backtest(
        data_dir, entry_price, stop_loss, max_spread, min_oi, cooldown,
        verbose=True, side=side, lookback_days=lookback_days,
        buy_if_bid_gt_entry=buy_gt, bid_limit_offset=offset,
    )

    print(f"\n{'='*70}")
    print(f"  Side:           {side}")
    print(f"  Total trades:   {summary['total_trades']}")
    print(f"  Total P/L:      {summary['total_pnl']:+.2f}¢ (${summary['total_pnl']/100:+.2f})")
    print(f"  Total cost:     {summary['total_cost']:.2f}¢ (${summary['total_cost']/100:.2f})")
    print(f"  Percent return: {summary['pct_return']:+.2f}%")
    ci = summary["pct_return_ci_95"]
    print(f"  95% CI (mean):  [{ci[0]:+.2f}%, {ci[1]:+.2f}%]")
    print(f"  Median return:  {summary['median_return']:+.2f}%")
    print(f"  % profitable markets: {summary['pct_profitable_markets']:.1f}%")
    print(f"  Sharpe-like:    {summary['sharpe_like']:.2f}")
    print(f"  t-stat:         {summary['t_stat']:.2f}")
    print(f"  Composite score: {summary['composite_score']:.2f}")
    print(f"  Wins / Losses:  {summary['wins']} / {summary['losses']}")
    print(f"  Win rate:       {summary['win_rate']:.1f}%")
    print(f"{'='*70}\n")


if __name__ == "__main__":
    main()
