"""
Sweep entry_price x stop_loss parameter space, run the backtest for each
combination, and report the best-performing configurations.

Usage:
    Edit the configuration section below, then run:
    python calibration/optimize.py

    Set LOOKBACK_DAYS to e.g. 5, 7, or 50 to restrict to markets whose ticker
    encodes an event date in that window (parsed from ``-YYMONDD...`` in the
    filename). Use None for all CSVs.
"""

import atexit
import csv
import json
import os
import signal
import sys
from datetime import date, datetime, timedelta

try:
    import colorama
    colorama.init()
except ImportError:
    colorama = None

from backtest_temp import load_markets_manifest, parse_event_date_from_ticker, run_backtest

# ANSI color codes (no-op if colorama not installed on Windows)
def _c(code: str) -> str:
    return f"\033[{code}m" if colorama else ""

C = {
    "reset": _c("0"),
    "bold": _c("1"),
    "dim": _c("2"),
    "red": _c("31"),
    "green": _c("32"),
    "yellow": _c("33"),
    "blue": _c("34"),
    "magenta": _c("35"),
    "cyan": _c("36"),
    "white": _c("37"),
}

# ---------------------------------------------------------------------------
# Configuration — edit these values directly
# ---------------------------------------------------------------------------
DATA_DIR = "calibration/past_data/KXBTC15M"
# Only include markets whose ticker encodes an event date in the last N calendar
# days (inclusive of today). None = all markets with a CSV.
LOOKBACK_DAYS = None    # e.g. 5, 7, or 50
ENTRY_MIN = 80       # Lowest entry_price to test
ENTRY_MAX = 99          # Highest entry_price to test 
STOP_MIN = 0            # Lowest stop_loss to test
STOP_MAX = 0            # Highest stop_loss to test
# MAX_SPREAD_LIST = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 50, 75, 76, 77, 78, 79, 80, 81, 82, 83,99]
MAX_SPREAD_LIST = [1]  # Max bid-ask spread values to test (cents)
# MIN_OPEN_INTEREST_LIST = [8000, 9000, 10000, 11000, 12000, 13000, 14000, 15000, 16000, 17000, 18000, 19000, 20000]  # Open interest thresholds to test (None = no filter)
MIN_OPEN_INTEREST_LIST = [5000]  # Open interest thresholds to test (None = no filter)
COOLDOWN_SECONDS_LIST = [0]  # Cooldown values to test (seconds after stop-loss)
# Three separate strings — NOT one string like "yes, no, both" (that would be a single invalid side).
SIDE_LIST = ["both"]    # Each value is swept as its own row
TOP_N = 20              # Number of top results to display
TOP_N_TO_TEST = 5       # Number of top configs to test on test set (when SETTING="both")
RESULTS_DIR = "calibration/sweep_results"

SETTING = "both"       # "training" | "testing" | "both"
TRAIN_RATIO = 0.7      # Oldest fraction of markets (by ticker event date) → train; rest → test
BEST_PARAMS_FILE = "calibration/sweep_results/best_params.json"


def _split_tickers(
    data_dir: str,
    train_ratio: float,
    lookback_days: int | None = None,
    as_of: date | None = None,
) -> tuple[set[str], set[str]]:
    """Split tickers into train (older events) and test (more recent).

    Only includes tickers with CSV files. If *lookback_days* is a positive int,
    only tickers whose event date (parsed from the ticker, ``-YYMONDD...``) falls
    in the last *lookback_days* calendar days (inclusive of *as_of*, default
    today) are eligible.

    Eligible tickers are sorted by event date ascending (oldest first). The first
    *train_ratio* fraction is training; the remainder is test. Tickers with no
    parseable event date are sorted after all dated tickers (usually in test).
    """
    manifest = load_markets_manifest(data_dir)
    ref = as_of or date.today()
    if lookback_days is not None and lookback_days > 0:
        cutoff = ref - timedelta(days=max(0, lookback_days - 1))
    else:
        cutoff = None

    tickers: list[str] = []
    for t in manifest:
        if not os.path.exists(os.path.join(data_dir, f"{t}.csv")):
            continue
        if cutoff is not None:
            ev = parse_event_date_from_ticker(t)
            if ev is None or ev < cutoff:
                continue
        tickers.append(t)

    def _chronological_key(ticker: str) -> tuple[int, date]:
        ev = parse_event_date_from_ticker(ticker)
        if ev is None:
            return (1, date.max)
        return (0, ev)

    tickers.sort(key=_chronological_key)
    n_train = max(1, int(len(tickers) * train_ratio))
    train_tickers = set(tickers[:n_train])
    test_tickers = set(tickers[n_train:])
    return train_tickers, test_tickers


def _load_best_params(path: str, default_max_spread: int = 1) -> dict:
    """Load best params from JSON file."""
    if not os.path.exists(path):
        raise SystemExit(
            f"Best params file not found: {path}\n"
            "Run with SETTING='training' or SETTING='both' first."
        )
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    for key in ("entry_price", "stop_loss", "cooldown_seconds"):
        if key not in data:
            raise SystemExit(f"Invalid best_params.json: missing '{key}'")
    if "max_spread" not in data:
        data["max_spread"] = default_max_spread
    return data


def _save_best_params(path: str, entry_price: int, stop_loss: int,
                      cooldown_seconds: int, max_spread: int,
                      min_open_interest: int | None = None,
                      side: str = "no") -> None:
    """Save best params to JSON file."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump({
            "entry_price": entry_price,
            "stop_loss": stop_loss,
            "cooldown_seconds": cooldown_seconds,
            "max_spread": max_spread,
            "min_open_interest": min_open_interest,
            "side": side,
        }, f, indent=2)


def _format_results_txt(results: list[dict], settings: dict, top: int,
                        completed: int, total: int) -> str:
    """Build the full text content for a sweep results file."""
    lines: list[str] = []

    lines.append(f"Sweep Run: {settings['timestamp']}")
    if completed < total:
        lines.append(f"*** INTERRUPTED — {completed}/{total} combinations completed ***")
    lines.append("")

    lines.append("Settings:")
    lines.append(f"  Data dir:           {settings['data_dir']}")
    if settings.get("lookback_days") is not None:
        lines.append(f"  Lookback (days):    {settings['lookback_days']}")
    if "train_markets" in settings:
        lines.append(f"  Train markets:      {settings['train_markets']}")
    if "test_markets" in settings:
        lines.append(f"  Test markets:       {settings['test_markets']}")
    if settings.get("train_ratio") is not None:
        tr = settings["train_ratio"]
        lines.append(
            f"  Train/test split:   chronological by event date (oldest {tr:.0%} → train, rest → test)"
        )
    lines.append(f"  Entry range:        {settings['entry_min']}–{settings['entry_max']}")
    lines.append(f"  Stop-loss range:    {settings['stop_min']}–{settings['stop_max']}")
    lines.append(f"  Max spread list:   {settings['max_spread_list']}¢")
    lines.append(f"  Min OI list:        {settings['min_oi_list']}")
    lines.append(f"  Cooldown values:    {settings['cooldown_list']}")
    lines.append(f"  Side list:          {settings.get('side_list', ['no'])}")
    lines.append(f"  Combinations:       {completed}/{total}")
    lines.append("")

    sorted_results = sorted(results, key=lambda r: r["composite_score"], reverse=True)

    lines.append(f"{'='*135}")
    lines.append(f"  TOP {min(top, len(sorted_results))} PARAMETER COMBINATIONS (by composite score)")
    lines.append(f"{'='*135}")
    lines.append(
        f"  {'Rank':>4s}  {'Side':>4s}  {'Entry':>5s}  {'Stop':>4s}  {'Cool':>5s}  {'Spread':>6s}  {'MinOI':>6s}  "
        f"{'Composite':>9s}  {'Return%':>7s}  {'95% CI':>14s}  {'Sharpe':>6s}  {'t-Stat':>6s}  "
        f"{'P/L':>8s}  {'Cost':>8s}  {'Trades':>6s}  {'WinRate':>7s}"
    )
    lines.append(
        f"  {'-'*4:>4s}  {'-'*4:>4s}  {'-'*5:>5s}  {'-'*4:>4s}  {'-'*5:>5s}  {'-'*6:>6s}  {'-'*6:>6s}  "
        f"{'-'*9:>9s}  {'-'*7:>7s}  {'-'*14:>14s}  {'-'*6:>6s}  {'-'*6:>6s}  "
        f"{'-'*8:>8s}  {'-'*8:>8s}  {'-'*6:>6s}  {'-'*7:>7s}"
    )

    for rank, r in enumerate(sorted_results[:top], 1):
        ci = r.get("pct_return_ci_95", (0.0, 0.0))
        ci_str = f"[{ci[0]:+.1f},{ci[1]:+.1f}]"
        moi = r.get("min_open_interest")
        moi_str = f"{moi:6d}" if moi is not None else "  None"
        side_str = r.get("side", "no")
        lines.append(
            f"  {rank:4d}  {side_str:>4s}  {r['entry_price']:5d}¢ {r['stop_loss']:4d}¢ "
            f" {r['cooldown_seconds']:4d}s  {r['max_spread']:5d}¢  {moi_str}  {r['composite_score']:9.2f}  "
            f"{r['pct_return']:+6.2f}%  {ci_str:>14s}  {r['sharpe_like']:6.2f}  {r['t_stat']:6.2f}  "
            f"{r['total_pnl']:+8.2f}¢  {r['total_cost']:8.2f}¢  {r['total_trades']:6d}  "
            f"{r['win_rate']:6.1f}%"
        )

    lines.append(f"{'='*135}")
    lines.append("")
    return "\n".join(lines)


def _save_results(path: str, results: list[dict], settings: dict,
                  top: int, completed: int, total: int) -> None:
    """Write the results .txt file."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    txt = _format_results_txt(results, settings, top, completed, total)
    with open(path, "w", encoding="utf-8") as f:
        f.write(txt)


def _run_training(data_dir: str, train_tickers: set[str], settings: dict,
                  combos: list, top: int, results_dir: str, slug: str,
                  timestamp: str, best_params_file: str) -> tuple[list[dict], dict | None]:
    """Run sweep on train split, save best params, return results and best params."""
    total = len(combos)
    results: list[dict] = []
    completed = 0
    results_file = os.path.join(results_dir, f"{slug}_{timestamp}_train.txt")

    def _dump_on_exit():
        if results:
            _save_results(results_file, results, settings, top, completed, total)
            print(f"\n{C['cyan']}Results saved to{C['reset']} {results_file}")

    atexit.register(_dump_on_exit)

    print(f"{C['cyan']}{C['bold']}Sweeping {total} (entry_price, stop_loss, cooldown, max_spread, min_oi, side) combinations on TRAIN split{C['reset']} …")
    print(f"  Data dir:   {data_dir}")
    if settings.get("lookback_days") is not None:
        print(f"  Lookback:   last {settings['lookback_days']} calendar day(s)")
    print(f"  Test markets (held out, more recent): {settings['test_markets']}")
    print(f"  Max spread list: {settings['max_spread_list']}¢  min_oi_list: {settings['min_oi_list']}  "
          f"cooldown: {settings['cooldown_list']}  side: {settings.get('side_list', ['no'])}\n")

    bar_width = 40
    for i, (entry_price, stop_loss, cooldown, max_spread, min_oi, side) in enumerate(combos, 1):
        summary = run_backtest(
            data_dir, entry_price, stop_loss, max_spread, min_oi, cooldown,
            ticker_filter=train_tickers, side=side,
        )
        results.append({
            "entry_price": entry_price,
            "stop_loss": stop_loss,
            "cooldown_seconds": cooldown,
            "max_spread": max_spread,
            "min_open_interest": min_oi,
            "side": side,
            "pct_return": summary["pct_return"],
            "pct_return_ci_95": summary.get("pct_return_ci_95", (0.0, 0.0)),
            "total_pnl": summary["total_pnl"],
            "total_cost": summary["total_cost"],
            "total_trades": summary["total_trades"],
            "win_rate": summary["win_rate"],
            "median_return": summary["median_return"],
            "pct_profitable_markets": summary["pct_profitable_markets"],
            "sharpe_like": summary["sharpe_like"],
            "t_stat": summary["t_stat"],
            "composite_score": summary["composite_score"],
        })
        completed = i

        pct = i / total
        filled = int(bar_width * pct)
        bar = f"{C['green']}{'#' * filled}{C['reset']}{C['dim']}{'-' * (bar_width - filled)}{C['reset']}"
        score = summary['composite_score']
        score_color = C['green'] if score > 0 else (C['red'] if score < 0 else "")
        print(
            f"\r  [{bar}] {C['cyan']}{pct:6.1%}{C['reset']}  ({i}/{total})  "
            f"side={side} entry={entry_price} stop={stop_loss} cool={cooldown}s spread={max_spread}¢ "
            f"-> composite={score_color}{score:.2f}{C['reset']}",
            end="", flush=True,
        )

    print()
    results.sort(key=lambda r: r["composite_score"], reverse=True)
    best = results[0]
    _save_best_params(
        best_params_file,
        best["entry_price"],
        best["stop_loss"],
        best["cooldown_seconds"],
        best["max_spread"],
        best.get("min_open_interest"),
        best.get("side", "no"),
    )
    print(f"{C['green']}{C['bold']}Best params saved{C['reset']} to {best_params_file}: "
          f"side={best.get('side', 'no')} entry={best['entry_price']}¢ stop={best['stop_loss']}¢ "
          f"cooldown={best['cooldown_seconds']}s spread={best['max_spread']}¢ "
          f"min_oi={best.get('min_open_interest')}")

    return results, best


def _fmt_num(val: float, fmt: str = "+6.2f", suffix: str = "") -> str:
    """Format number with color: green if positive, red if negative. Suffix (% or ¢) gets same color."""
    s = f"{val:{fmt}}{suffix}"
    if val > 0:
        return f"{C['green']}{s}{C['reset']}"
    if val < 0:
        return f"{C['red']}{s}{C['reset']}"
    return s


def _fmt_ci(ci: tuple[float, float], width: int | None = 14, prec: str = ".1f") -> str:
    """Return colored CI string: white if includes 0, red if negative, green if positive."""
    low, high = ci[0], ci[1]
    s = f"[{low:+{prec}},{high:+{prec}}]"
    padded = f"{s:>{width}s}" if width else s
    if low <= 0 <= high:
        color = C["white"]
    elif high < 0:
        color = C["red"]
    else:
        color = C["green"]
    return f"{color}{padded}{C['reset']}"


def _fmt_sharpe(val: float) -> str:
    """Format Sharpe: green if > 2, else default."""
    s = f"{val:6.2f}"
    return f"{C['green']}{s}{C['reset']}" if val > 2 else s


def _fmt_tstat(val: float) -> str:
    """Format t-stat: green if > 2, else default. Handles inf."""
    s = f"{val:6.2f}" if val != float("inf") else "   inf"
    return f"{C['green']}{s}{C['reset']}" if val > 2 else s


def _print_results_table(results: list[dict], top: int, title: str) -> None:
    """Print the top-N results table."""
    sep = f"{C['dim']}{'='*135}{C['reset']}"
    print(f"\n{sep}")
    print(f"  {C['cyan']}{C['bold']}{title}{C['reset']}")
    print(sep)
    print(
        f"  {C['bold']}{'Rank':>4s}  {'Side':>4s}  {'Entry':>5s}  {'Stop':>4s}  {'Cool':>5s}  {'Spread':>6s}  {'MinOI':>6s}  "
        f"{'Composite':>9s}  {'Return%':>7s}  {'95% CI':>14s}  {'Sharpe':>6s}  {'t-Stat':>6s}  {'P/L':>8s}  "
        f"{'Cost':>8s}  {'Trades':>6s}  {'WinRate':>7s}{C['reset']}"
    )
    print(
        f"  {C['dim']}{'-'*4:>4s}  {'-'*4:>4s}  {'-'*5:>5s}  {'-'*4:>4s}  {'-'*5:>5s}  {'-'*6:>6s}  {'-'*6:>6s}  "
        f"{'-'*9:>9s}  {'-'*7:>7s}  {'-'*14:>14s}  {'-'*6:>6s}  {'-'*6:>6s}  {'-'*8:>8s}  "
        f"{'-'*8:>8s}  {'-'*6:>6s}  {'-'*7:>7s}{C['reset']}"
    )
    for rank, r in enumerate(results[:top], 1):
        rank_style = f"{C['yellow']}{C['bold']}" if rank == 1 else ""
        rank_reset = C["reset"] if rank == 1 else ""
        comp_color = C['green'] if r['composite_score'] > 0 else (C['red'] if r['composite_score'] < 0 else "")
        ci = r.get("pct_return_ci_95", (0.0, 0.0))
        ci_str = _fmt_ci(ci)
        sharpe_str = _fmt_sharpe(r['sharpe_like'])
        tstat_str = _fmt_tstat(r['t_stat'])
        max_spread = r.get("max_spread", 0)
        moi = r.get("min_open_interest")
        moi_str = f"{moi:6d}" if moi is not None else "  None"
        side_str = r.get("side", "no")
        print(
            f"  {rank_style}{rank:4d}{rank_reset}  {side_str:>4s}  {r['entry_price']:5d}¢ {r['stop_loss']:4d}¢ "
            f" {r['cooldown_seconds']:4d}s  {max_spread:5d}¢  {moi_str}  {comp_color}{r['composite_score']:9.2f}{C['reset']}  "
            f"{_fmt_num(r['pct_return'], suffix='%')}  {ci_str}  {sharpe_str}  {tstat_str}  "
            f"{_fmt_num(r['total_pnl'], '+8.2f', suffix='¢')}  {r['total_cost']:8.2f}¢  {r['total_trades']:6d}  "
            f"{r['win_rate']:6.1f}%"
        )
    print(f"{sep}\n")


def main():
    data_dir = DATA_DIR
    entry_min = ENTRY_MIN
    entry_max = ENTRY_MAX
    stop_min = STOP_MIN
    stop_max = STOP_MAX
    max_spread_list = MAX_SPREAD_LIST
    min_oi_list = MIN_OPEN_INTEREST_LIST
    cooldown_list = COOLDOWN_SECONDS_LIST
    side_list = SIDE_LIST
    top = TOP_N
    top_n_to_test = TOP_N_TO_TEST
    results_dir = RESULTS_DIR
    setting = SETTING
    train_ratio = TRAIN_RATIO
    best_params_file = BEST_PARAMS_FILE
    lookback_days = LOOKBACK_DAYS

    if setting not in ("training", "testing", "both"):
        raise SystemExit(f"Invalid SETTING: {setting!r}. Use 'training', 'testing', or 'both'.")

    _valid_sides = frozenset({"yes", "no", "both"})
    for _sd in side_list:
        if _sd not in _valid_sides:
            raise SystemExit(
                f"Invalid SIDE_LIST entry {_sd!r}. Each item must be exactly 'yes', 'no', or 'both'. "
                f"Use three list elements, e.g. [\"yes\", \"no\", \"both\"] — not one string \"yes, no, both\"."
            )

    train_tickers, test_tickers = _split_tickers(
        data_dir, train_ratio, lookback_days=lookback_days,
    )
    if not train_tickers and not test_tickers:
        raise SystemExit(
            "No markets after applying LOOKBACK_DAYS and CSV filter. "
            "Try LOOKBACK_DAYS = None or a larger window."
        )
    print(
        f"{C['cyan']}Markets used —{C['reset']} "
        f"training: {C['bold']}{len(train_tickers)}{C['reset']} (older by event date), "
        f"testing: {C['bold']}{len(test_tickers)}{C['reset']} (more recent)\n"
    )
    timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    slug = os.path.basename(data_dir)

    settings = {
        "data_dir": data_dir,
        "entry_min": entry_min,
        "entry_max": entry_max,
        "stop_min": stop_min,
        "stop_max": stop_max,
        "max_spread_list": max_spread_list,
        "min_oi_list": min_oi_list,
        "cooldown_list": cooldown_list,
        "side_list": side_list,
        "timestamp": timestamp,
        "train_markets": len(train_tickers),
        "test_markets": len(test_tickers),
        "train_ratio": train_ratio,
        "lookback_days": lookback_days,
    }

    combos = [
        (ep, sl, cd, ms, moi, sd)
        for ep in range(entry_min, entry_max + 1)
        for sl in range(stop_min, stop_max + 1)
        for cd in cooldown_list
        for ms in max_spread_list
        for moi in min_oi_list
        for sd in side_list
        if sl < ep
    ]

    def _signal_handler(sig, frame):
        print(f"\n\n{C['yellow']}Interrupted (signal {sig}) — saving partial results …{C['reset']}")
        sys.exit(1)

    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)

    if setting == "testing":
        params = _load_best_params(best_params_file, default_max_spread=max_spread_list[0] if max_spread_list else 1)
        test_min_oi = params.get("min_open_interest")
        test_side = params.get("side", "no")
        print(f"{C['cyan']}{C['bold']}Testing best params on TEST split{C['reset']} ({len(test_tickers)} markets)\n")
        summary = run_backtest(
            data_dir,
            params["entry_price"],
            params["stop_loss"],
            params["max_spread"],
            test_min_oi,
            params["cooldown_seconds"],
            ticker_filter=test_tickers,
            side=test_side,
        )
        test_results = [{**summary, "cooldown_seconds": params["cooldown_seconds"], "max_spread": params["max_spread"],
                         "min_open_interest": test_min_oi, "side": test_side}]
        _print_results_table(
            test_results, 1,
            "TEST PERFORMANCE (best params from training)",
        )
        return

    # training or both
    results, best = _run_training(
        data_dir, train_tickers, settings, combos,
        top, results_dir,
        slug, timestamp, best_params_file,
    )

    _print_results_table(
        results, top,
        f"TOP {top} PARAMETER COMBINATIONS (by composite score) — TRAIN split",
    )

    # Save full results CSV
    sweep_csv = os.path.join(results_dir, f"{slug}_{timestamp}_train.csv")
    os.makedirs(results_dir, exist_ok=True)
    fieldnames = [
        "side", "entry_price", "stop_loss", "cooldown_seconds", "max_spread", "min_open_interest",
        "pct_return",
        "pct_return_ci_low", "pct_return_ci_high", "median_return",
        "pct_profitable_markets", "sharpe_like", "t_stat", "composite_score",
        "total_pnl", "total_cost", "total_trades", "win_rate",
    ]
    rows_for_csv = [
        {
            **r,
            "pct_return_ci_low": r["pct_return_ci_95"][0],
            "pct_return_ci_high": r["pct_return_ci_95"][1],
        }
        for r in results
    ]
    with open(sweep_csv, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows_for_csv)
    print(f"{C['cyan']}Full sweep results saved to{C['reset']} {sweep_csv}")

    if setting == "both":
        n_to_test = min(top_n_to_test, len(results))
        print(f"\n{C['magenta']}--- Test performance of top {n_to_test} configs ---{C['reset']}\n")
        test_results = []
        for r in results[:n_to_test]:
            r_side = r.get("side", "no")
            summary = run_backtest(
                data_dir,
                r["entry_price"],
                r["stop_loss"],
                r["max_spread"],
                r.get("min_open_interest"),
                r["cooldown_seconds"],
                ticker_filter=test_tickers,
                side=r_side,
            )
            test_results.append({**summary, "cooldown_seconds": r["cooldown_seconds"], "max_spread": r["max_spread"],
                                 "min_open_interest": r.get("min_open_interest"), "side": r_side})
        _print_results_table(
            test_results, n_to_test,
            f"TEST PERFORMANCE (top {n_to_test} from training)",
        )


if __name__ == "__main__":
    main()
