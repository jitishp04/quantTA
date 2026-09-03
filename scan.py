#!/usr/bin/env python3
"""Barbell scanner entrypoint.

Fetches every watchlist ticker on each configured timeframe, computes the fixed
indicator stack, evaluates the capitulation / euphoria conditions and pushes a
single consolidated Discord message.

    python scan.py                          # full scan, sends to Discord
    python scan.py --dry-run                # print to stdout, send nothing
    python scan.py --tickers SPY,QQQ        # ad-hoc universe
    python scan.py --timeframe daily        # one environment only
    python scan.py --force                  # ignore de-duplication state
    python scan.py --show-all               # include NEUTRAL rows (diagnostics)
"""

from __future__ import annotations

import argparse
import logging
import sys

from src.config import load_dotenv
from src.data import fetch
from src.indicators import compute
from src.notify import DiscordClient, describe, format_alerts, format_heartbeat
from src.signals import NEUTRAL, Snapshot, evaluate
from src.state import AlertState
from src.watchlist import load_settings, load_tickers, normalise

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("scan")

load_dotenv()   # local convenience; real env vars always take precedence


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Barbell extremes scanner")
    parser.add_argument("--dry-run", action="store_true",
                        help="print the alert payload instead of sending it")
    parser.add_argument("--tickers", type=str,
                        help="comma-separated override for the watchlist")
    parser.add_argument("--timeframe", choices=["daily", "weekly"],
                        help="scan a single timeframe instead of both")
    parser.add_argument("--force", action="store_true",
                        help="alert on every active signal, ignoring cooldown state")
    parser.add_argument("--show-all", action="store_true",
                        help="log every ticker's reading, including neutral ones")
    return parser.parse_args()


def scan_timeframe(
    tickers: list[str],
    timeframe: str,
    batch_size: int,
) -> tuple[list[Snapshot], dict[str, str]]:
    """Run one full timeframe pass. Returns (snapshots, problems)."""
    log.info("[%s] downloading %d tickers", timeframe, len(tickers))
    frames, problems = fetch(tickers, timeframe, batch_size=batch_size)
    for ticker, reason in problems.items():
        log.warning("[%s] %s: %s", timeframe, ticker, reason)

    snapshots: list[Snapshot] = []
    for ticker, frame in frames.items():
        try:
            snap = evaluate(compute(frame), ticker, timeframe)
        except Exception as exc:
            problems[ticker] = f"{timeframe} indicator error: {exc}"
            continue
        if snap is None:
            problems[ticker] = f"{timeframe} indicators not warmed up"
            continue
        snapshots.append(snap)

    log.info("[%s] evaluated %d, skipped %d", timeframe, len(snapshots), len(problems))
    return snapshots, problems


def main() -> int:
    args = parse_args()
    settings = load_settings()

    if args.tickers:
        tickers = [s for s in (normalise(t) for t in args.tickers.split(",")) if s]
    else:
        tickers = load_tickers()

    if not tickers:
        log.error("watchlist is empty -- add symbols to config/tickers.txt")
        return 1

    timeframes = [args.timeframe] if args.timeframe else list(settings["timeframes"])

    all_snaps: list[Snapshot] = []
    problems: dict[str, str] = {}
    for timeframe in timeframes:
        snaps, issues = scan_timeframe(tickers, timeframe, settings["batch_size"])
        all_snaps.extend(snaps)
        for ticker, reason in issues.items():
            problems.setdefault(ticker, reason)

    if args.show_all:
        for snap in sorted(all_snaps, key=lambda s: (s.timeframe, s.ticker)):
            log.info(
                "%-12s %-6s %-12s close=%-12.4f rsi=%-6.2f %%B=%.3f",
                snap.ticker, snap.timeframe, snap.signal,
                snap.close, snap.rsi, snap.percent_b,
            )

    # De-duplicate against prior runs so a persistent condition does not fire daily.
    state = AlertState()
    cooldown = int(settings["cooldown_days"])
    alerts: list[Snapshot] = []
    for snap in all_snaps:
        if args.force:
            if snap.signal != NEUTRAL:
                alerts.append(snap)
                state.record(snap)
        elif state.should_alert(snap, cooldown):
            alerts.append(snap)
            state.record(snap)

    reported = problems if settings.get("report_problems") else None

    if alerts:
        payload = format_alerts(alerts, reported)
    elif settings.get("heartbeat"):
        payload = format_heartbeat(len(tickers), timeframes, reported)
    else:
        log.info("no alerts, heartbeat disabled -- nothing to send")
        if not args.dry_run:
            state.save()
        return 0

    if args.dry_run:
        print("\n" + "=" * 68)
        print(describe(payload))
        print("=" * 68)
        log.info("dry run: %d alert(s) not sent, state not written", len(alerts))
        return 0

    DiscordClient().send(payload)
    state.save()
    log.info("sent %d alert(s)", len(alerts))
    return 0


if __name__ == "__main__":
    sys.exit(main())
