#!/usr/bin/env python3
"""Signal study entrypoint -- historical edge plus forward simulation.

Two independent layers, deliberately kept separate:

  Layer 1  What actually happened after this signal fired, historically,
           measured against simply holding the instrument.
  Layer 2  What a mean-reverting model projects from here, with an explicit
           test of whether that model is even applicable.

Layer 1 is evidence. Layer 2 is a model. Layer 2 is only worth reading when
Layer 1 says the signal has an edge worth modelling.

    python study.py                                  watchlist, daily, both signals
    python study.py --tickers SPY,QQQ --timeframe weekly
    python study.py --signal capitulation --horizon 126
    python study.py --no-mc                          historical study only
    python study.py --out study.md                   also write a markdown report
    python study.py --tickers NVDA --discord          also post to Discord
"""

from __future__ import annotations

import argparse
import logging
import sys

from src import backtest as bt
from src import montecarlo as mc
from src.config import load_dotenv
from src.data import fetch
from src.indicators import compute
from src.notify import DiscordClient
from src.signals import CAPITULATION, EUPHORIA
from src.watchlist import load_settings, load_tickers, normalise

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("study")

load_dotenv()   # local convenience; real env vars always take precedence

STUDY_YEARS = 50.0     # "everything Yahoo has"; short listings just return less
RULE = "=" * 78
THIN = "-" * 78


def pct(value: float, places: int = 2) -> str:
    if value != value:  # NaN
        return "   n/a"
    return f"{value * 100:+.{places}f}%"


def render_backtest(result: bt.BacktestResult, out: list[str]) -> None:
    name = "CAPITULATION" if result.signal == CAPITULATION else "EUPHORIA"
    out.append(f"\nLAYER 1 - {name}: {result.n_events} historical event(s)")

    if not result.event_dates:
        out.append("  none in the available history -- nothing to measure")
        return

    shown = result.event_dates[-6:]
    prefix = "  most recent: " if len(result.event_dates) > 6 else "  dates: "
    out.append(prefix + ", ".join(shown))

    if not result.stats:
        out.append("  history too short for any forward horizon")
        return

    unit = "d" if result.timeframe == "daily" else "w"
    out.append("")
    out.append(f"  {'horizon':>8}  {'n':>3}  {'win%':>6}  {'median':>8}  {'mean':>8}"
               f"  {'vs hold':>8}  {'MAE med':>8}  {'MAE worst':>9}  {'p':>6}")
    out.append("  " + "-" * 74)
    for s in result.stats:
        flag = " *" if s.significant else ""
        out.append(
            f"  {str(s.horizon) + unit:>8}  {s.n_events:>3}  {s.win_rate * 100:>5.1f}%"
            f"  {pct(s.median_return):>8}  {pct(s.mean_return):>8}"
            f"  {pct(s.edge_mean):>8}  {pct(s.median_mae):>8}"
            f"  {pct(s.worst_mae):>9}  {s.p_value:>5.3f}{flag}"
        )

    first = result.stats[0]
    out.append("")
    out.append(f"  baseline (all {first.baseline_n} warmed bars, unconditional):")
    for s in result.stats:
        out.append(f"    {str(s.horizon) + unit:>4}  win {s.baseline_win_rate * 100:5.1f}%"
                   f"   mean {pct(s.baseline_mean)}   median {pct(s.baseline_median)}")

    out.append("")
    out.append("  'vs hold' is the number that matters: conditional mean minus the")
    out.append("  unconditional mean over the same history. A high win rate with a")
    out.append("  'vs hold' near zero means the signal added nothing.")
    if result.note:
        out.append(f"  NOTE: {result.note}")


def render_montecarlo(result: mc.MonteCarloResult, out: list[str]) -> None:
    fit = result.fit
    unit = "candles" if result.timeframe == "daily" else "weeks"
    out.append(f"\nLAYER 2 - OU MONTE CARLO ({result.horizon} {unit}, "
               f"{result.n_paths:,} paths)")

    verdict = "mean-reverting" if fit.is_mean_reverting else "NOT mean-reverting"
    half = f"{fit.half_life:.1f}" if fit.half_life != float("inf") else "inf"
    out.append(f"  fit: half-life {half} {unit} - theta {fit.theta:.4f}"
               f" - DF t {fit.df_stat:+.2f} ({verdict}) - R2 {fit.r_squared:.3f}"
               f" - n {fit.n_obs}")
    out.append(f"  spot {result.spot:,.2f} - baseline BBM {result.baseline:,.2f}"
               f" - spread {pct(result.spread_now)} (log)")

    if not fit.is_mean_reverting:
        out.append("")
        out.append("  ** The spread failed the Dickey-Fuller test at 5%, so this series")
        out.append("     is behaving as a random walk around its baseline, not as a")
        out.append("     mean-reverting process. The projection below is the model")
        out.append("     talking, not the data. Do not read it as a probability. **")
    elif not result.reversion_actionable:
        out.append("")
        out.append(f"  ** Mean-reverting, but the half-life ({half} {unit}) is longer")
        out.append(f"     than the {result.horizon}-{unit[0]} horizon: less than half the gap")
        out.append("     closes in the time being modelled. Statistically real,")
        out.append("     practically slow -- lengthen the horizon or size accordingly. **")

    out.append("")
    percentiles = result.return_percentiles
    out.append("  forward return   " + "  ".join(
        f"p{p} {pct(percentiles[p], 1):>8}" for p in sorted(percentiles)))
    reach = ("never" if result.median_bars_to_baseline is None
             else f"median {result.median_bars_to_baseline:.0f} {unit}")
    out.append(f"  P(profit) {result.prob_profit * 100:.1f}%"
               f"   P(back to mid-band) {result.prob_reach_baseline * 100:.1f}% ({reach})")
    out.append(f"  P(drawdown -10%) {result.prob_drawdown_10 * 100:.1f}%"
               f"   P(-20%) {result.prob_drawdown_20 * 100:.1f}%"
               f"   ES(worst 5%) {pct(result.expected_shortfall_5)}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Historical edge and forward simulation")
    parser.add_argument("--tickers", help="comma-separated override for the watchlist")
    parser.add_argument("--timeframe", choices=["daily", "weekly"], default="daily")
    parser.add_argument("--signal", choices=["capitulation", "euphoria", "both"],
                        default="both")
    parser.add_argument("--horizon", type=int,
                        help="Monte Carlo horizon in candles (default: 3-month)")
    parser.add_argument("--paths", type=int, default=mc.DEFAULT_PATHS)
    parser.add_argument("--no-backtest", action="store_true")
    parser.add_argument("--no-mc", action="store_true")
    parser.add_argument("--out", help="also write the report to this file")
    parser.add_argument("--discord", action="store_true",
                        help="also post the report to Discord (requires "
                             "DISCORD_WEBHOOK_URL)")
    args = parser.parse_args()

    settings = load_settings()
    if args.tickers:
        tickers = [s for s in (normalise(t) for t in args.tickers.split(",")) if s]
    else:
        tickers = load_tickers()
    if not tickers:
        log.error("no tickers to study")
        return 1

    signals = ([CAPITULATION, EUPHORIA] if args.signal == "both"
               else [CAPITULATION if args.signal == "capitulation" else EUPHORIA])
    horizon = args.horizon or bt.HORIZONS[args.timeframe][1]   # ~3 months

    log.info("fetching full %s history for %d ticker(s)", args.timeframe, len(tickers))
    frames, problems = fetch(
        tickers, args.timeframe,
        batch_size=settings["batch_size"], years=STUDY_YEARS, trim=False,
    )
    for ticker, reason in problems.items():
        log.warning("%s: %s", ticker, reason)
    if not frames:
        log.error("no usable history for any ticker")
        return 1

    out: list[str] = []
    for ticker in tickers:
        if ticker not in frames:
            continue
        try:
            enriched = compute(frames[ticker])
        except ValueError as exc:
            log.warning("%s: %s", ticker, exc)
            continue

        out.append("")
        out.append(RULE)
        out.append(f"{ticker} - {args.timeframe.upper()}")
        out.append(f"history {enriched.index[0]:%Y-%m-%d} -> {enriched.index[-1]:%Y-%m-%d}"
                   f"  ({len(enriched):,} bars)")
        out.append(THIN)

        if not args.no_backtest:
            for signal in signals:
                render_backtest(bt.run(enriched, ticker, args.timeframe, signal), out)

        if not args.no_mc:
            try:
                render_montecarlo(
                    mc.simulate(enriched, ticker, args.timeframe, horizon,
                                n_paths=args.paths),
                    out,
                )
            except ValueError as exc:
                out.append(f"\nLAYER 2 - unavailable: {exc}")

    out.append("")
    out.append(RULE)
    out.append("Layer 1 is evidence; Layer 2 is a model. Overlapping forward windows")
    out.append("and clustered events make the p-values optimistic -- treat a small")
    out.append("event count as descriptive, never as significance.")

    report = "\n".join(out)
    print(report)
    if args.out:
        from pathlib import Path
        Path(args.out).write_text(report + "\n", encoding="utf-8")
        log.info("report written to %s", args.out)

    if args.discord:
        # A failed post should not turn a successful study into a red X in
        # Actions -- the job summary and artifact already have the report as
        # a fallback, so this degrades to a logged warning instead.
        try:
            DiscordClient().send_code_block(report)
            log.info("report posted to Discord")
        except Exception as exc:
            log.warning("could not post report to Discord: %s", exc)

    return 0


if __name__ == "__main__":
    sys.exit(main())
