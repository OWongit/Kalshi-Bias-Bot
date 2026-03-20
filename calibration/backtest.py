"""
Standalone backtest of the NO-entry / stop-loss strategy on historical
candlestick CSVs.

Usage:
    python calibration/backtest.py calibration/past_data/KXNCAAMBGAME \
        --entry_price 95 --stop_loss 70 --max_spread 2

No imports from the parent project — reads only CSV files.
"""

import csv
import math
import os
import statistics
import sys


def _safe_int(val):
    """Parse a CSV cell to int, returning None for empty / invalid values."""
    if val is None or val == "":
        return None
    try:
        return int(round(float(val)))
    except (ValueError, TypeError):
        return None


def load_markets_manifest(data_dir: str) -> dict[str, dict]:
    """Read _markets.csv and return {ticker: {result, open_time, close_time}}."""
    path = os.path.join(data_dir, "_markets.csv")
    manifest: dict[str, dict] = {}
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            manifest[row["ticker"]] = row
    return manifest


def load_candles(csv_path: str) -> list[dict]:
    """Read a market candlestick CSV into a list of dicts with int values."""
    candles: list[dict] = []
    with open(csv_path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            candles.append({k: _safe_int(v) for k, v in row.items()})
    return candles[:len(candles) -1] #remove the last row because it's incomplete.


def simulate_market(
    candles: list[dict],
    result: str,
    entry_price: int,
    stop_loss: int,
    max_spread: int,
    min_open_interest: int | None = None,
    cooldown_seconds: int = 0,
) -> list[dict]:
    """Simulate the NO strategy on one market's candle series.

    Returns a list of trade dicts, each with:
        entry_cost, exit_price, pnl, exit_reason
    """
    trades: list[dict] = []
    holding = False
    entry_cost = 0
    cooldown_until = 0

    for candle in candles:
        ts = candle.get("end_period_ts") or 0

        # Extract YES-side close prices and the YES ask high for the candle
        yes_bid_close = candle.get("yes_bid_close")
        yes_ask_close = candle.get("yes_ask_close")
        yes_ask_high = candle.get("yes_ask_high")

        # Skip candles with missing price data
        if yes_bid_close is None or yes_ask_close is None:
            continue

        # Derive NO-side prices from YES-side (binary market identity):
        #   no_bid = 100 - yes_ask  (best someone will pay for NO)
        #   no_ask = 100 - yes_bid  (cheapest someone will sell NO)
        no_bid_close = 100 - yes_ask_close
        no_ask_close = 100 - yes_bid_close

        if holding:
            # Only evaluate stop-loss on candles with actual trading activity
            vol = candle.get("volume") or 0
            if vol <= 0:
                continue
            if min_open_interest is not None:
                oi = candle.get("open_interest") or 0
                if oi < min_open_interest:
                    continue

            # Compute the worst (lowest) NO bid that occurred during this
            # candle.  YES ask at its HIGH means NO bid was at its LOW.
            if yes_ask_high is not None:
                no_bid_low = 100 - yes_ask_high
            else:
                no_bid_low = no_bid_close

            # Stop-loss trigger: if the NO bid dipped to or below the
            # threshold at any point during the candle, exit at the
            # stop-loss price.  P/L = exit_price - entry_cost.
            if no_bid_low <= stop_loss:
                pnl = stop_loss - entry_cost
                trades.append({
                    "entry_cost": entry_cost,
                    "exit_price": stop_loss,
                    "pnl": pnl,
                    "exit_reason": "stop_loss",
                })
                holding = False
                cooldown_until = ts + cooldown_seconds
                continue
        else:
            # Skip entry while in cooldown after a stop-loss
            if ts < cooldown_until:
                continue
            # --- Entry check ---
            # Require NO ask to exactly equal the configured entry price
            if no_ask_close != entry_price:
                continue
            # Require the NO bid-ask spread to be tight enough
            spread = no_ask_close - no_bid_close
            if spread > max_spread:
                continue
            # Require the NO bid to be a valid order price (1-99 cents)
            if no_bid_close < 1 or no_bid_close >= 100:
                continue
            # Require minimum open interest if configured
            if min_open_interest is not None:
                oi = candle.get("open_interest") or 0
                if oi < min_open_interest:
                    continue
            # Skip candles with zero volume -- no counterparty to fill
            vol = candle.get("volume") or 0
            if vol <= 0:
                continue
            # Enter: place a maker limit buy at the NO bid
            entry_cost = no_bid_close
            holding = True

    # Settlement: if still holding when the market resolves, determine
    # outcome from the market's result field.
    if holding:
        # result == "no" means the NO side wins -> payout is 100¢ per contract
        if result == "no":
            pnl = 100 - entry_cost
            exit_reason = "settlement_win"
        # result == "yes" means the YES side wins -> NO contracts expire worthless
        else:
            pnl = 0 - entry_cost
            exit_reason = "settlement_loss"
        trades.append({
            "entry_cost": entry_cost,
            "exit_price": 100 if result == "no" else 0,
            "pnl": pnl,
            "exit_reason": exit_reason,
        })

    return trades


def _compute_composite_stats(market_stats: list[dict], entry_price: int) -> dict:
    """Compute composite score statistics from per-market pnl/win stats.

    market_stats: list of dicts with keys: ticker, pnl, cost, trades, wins, losses
    Returns dict with: pct_return, pct_return_ci_95, win_rate, median_return,
    pct_profitable_markets, sharpe_like, t_stat, composite_score
    """
    # Markets with at least one trade (cost > 0)
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

    # 95% CI for mean per-market return: mean ± t_critical * (std / sqrt(n))
    # Use 1.96 for n >= 30 (normal approx), else ~2.0 for small samples
    t_critical = 1.96
    se = (std_return / math.sqrt(n)) if n > 0 else 0.0
    ci_low = mean_return - t_critical * se
    ci_high = mean_return + t_critical * se
    pct_return_ci_95 = (ci_low, ci_high)

    # Composite score: blend return quality with consistency and significance
    # Favors strategies with good median return, high % profitable markets,
    # and statistical significance (t_stat)
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
    entry_price: int,
    stop_loss: int,
    max_spread: int,
    min_open_interest: int | None = None,
    cooldown_seconds: int = 0,
    verbose: bool = False,
    ticker_filter: set[str] | None = None,
) -> dict:
    """Run the backtest over all markets in *data_dir*.

    Returns a summary dict with aggregate stats.
    """
    manifest = load_markets_manifest(data_dir)

    market_stats: list[dict] = []

    for ticker, meta in manifest.items():
        if ticker_filter is not None and ticker not in ticker_filter:
            continue
        csv_path = os.path.join(data_dir, f"{ticker}.csv")
        if not os.path.exists(csv_path):
            continue

        candles = load_candles(csv_path)
        if not candles:
            continue

        trades = simulate_market(
            candles, meta.get("result", ""), entry_price, stop_loss, max_spread,
            min_open_interest, cooldown_seconds,
        )

        mkt_pnl = sum(t["pnl"] for t in trades)
        mkt_cost = sum(t["entry_cost"] for t in trades)
        mkt_wins = sum(1 for t in trades if t["pnl"] > 0)
        mkt_losses = sum(1 for t in trades if t["pnl"] <= 0)

        if verbose and trades:
            pct = (mkt_pnl / mkt_cost * 100) if mkt_cost else 0.0
            print(
                f"  {ticker:40s}  trades={len(trades):3d}  "
                f"pnl={mkt_pnl:+6d}¢  cost={mkt_cost:6d}¢  "
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

    composite = _compute_composite_stats(market_stats, entry_price)

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
    entry_price: int,
    stop_loss: int,
    max_spread: int,
    min_open_interest: int | None = None,
    cooldown_seconds: int = 0,
    verbose: bool = False,
) -> dict:
    """Run the backtest on a single market CSV file.

    Args:
        csv_path:    Path to a market candlestick CSV.
        result:      Settlement result for this market ("yes" or "no").
        entry_price: NO ask must equal this to enter (cents).
        stop_loss:   Sell when NO bid drops to this (cents).
        max_spread:  Max NO bid-ask spread for entry (cents).
        min_open_interest: Skip candles with open_interest below this (None = no filter).
        cooldown_seconds:  Seconds to wait after a stop-loss before re-entering.
        verbose:     Print per-trade details.

    Returns a summary dict with stats for this single market.
    """
    candles = load_candles(csv_path)
    ticker = os.path.splitext(os.path.basename(csv_path))[0]

    trades = simulate_market(candles, result, entry_price, stop_loss, max_spread,
                             min_open_interest, cooldown_seconds)


    print(trades)
    total_pnl = sum(t["pnl"] for t in trades)
    total_cost = sum(t["entry_cost"] for t in trades)
    wins = sum(1 for t in trades if t["pnl"] > 0)
    losses = sum(1 for t in trades if t["pnl"] <= 0)
    pct_return = (total_pnl / total_cost * 100) if total_cost else 0.0
    win_rate = (wins / len(trades) * 100) if trades else 0.0

    if verbose:
        print(f"\nBacktest (single): {ticker}")
        print(f"  Result: {result}  |  entry={entry_price}¢  "
              f"stop_loss={stop_loss}¢  max_spread={max_spread}¢\n")
        for j, t in enumerate(trades, 1):
            print(f"  Trade {j}: buy @ {t['entry_cost']}¢  "
                  f"exit @ {t['exit_price']}¢  pnl={t['pnl']:+d}¢  "
                  f"({t['exit_reason']})")
        print(f"\n  Total trades:   {len(trades)}")
        print(f"  Total P/L:      {total_pnl:+d}¢ (${total_pnl/100:+.2f})")
        print(f"  Total cost:     {total_cost}¢ (${total_cost/100:.2f})")
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
ENTRY_PRICE = 95        # NO ask must equal this to enter (cents)
STOP_LOSS = 0          # Sell when NO bid drops to this (cents)
MAX_SPREAD = 1          # Max NO bid-ask spread for entry (cents)
MIN_OPEN_INTEREST = None  # Skip candles with open_interest below this (None = no filter)
COOLDOWN_SECONDS = 300  # Seconds to wait after a stop-loss before re-entering

# Set SINGLE_CSV to a file path to backtest just one market instead of the
# whole directory.  Set to None to run the full directory backtest.
# SINGLE_RESULT must be "yes" or "no" — the settlement outcome of that market.
#SINGLE_CSV = "calibration/past_data/KXNCAAMBGAME/KXNCAAMBGAME-25DEC01BGSUKSU-BGSU.csv"
SINGLE_CSV = None
SINGLE_RESULT = "no"    # settlement result for the single market


def main():
    entry_price = ENTRY_PRICE
    stop_loss = STOP_LOSS
    max_spread = MAX_SPREAD
    min_oi = MIN_OPEN_INTEREST
    cooldown = COOLDOWN_SECONDS

    if SINGLE_CSV:
        # Single-file mode
        run_backtest_single(
            SINGLE_CSV, SINGLE_RESULT, entry_price, stop_loss, max_spread,
            min_oi, cooldown, verbose=True,
        )
        return

    # Full directory mode
    data_dir = DATA_DIR
    print(f"\nBacktest: entry={entry_price}¢  stop_loss={stop_loss}¢  "
          f"max_spread={max_spread}¢  min_open_interest={min_oi}  "
          f"cooldown={cooldown}s")
    print(f"Data dir: {data_dir}\n")

    summary = run_backtest(
        data_dir, entry_price, stop_loss, max_spread, min_oi, cooldown,
        verbose=True,
    )

    print(f"\n{'='*70}")
    print(f"  Total trades:   {summary['total_trades']}")
    print(f"  Total P/L:      {summary['total_pnl']:+d}¢ (${summary['total_pnl']/100:+.2f})")
    print(f"  Total cost:     {summary['total_cost']}¢ (${summary['total_cost']/100:.2f})")
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
