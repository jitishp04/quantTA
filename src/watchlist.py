"""Watchlist and settings I/O.

The ticker list lives in a plain text file rather than inside settings.yml so
that the Discord sync job can rewrite it deterministically (sorted, unique,
uppercase) without destroying hand-written YAML comments.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

import yaml

from .config import SETTINGS_FILE, TICKERS_FILE

log = logging.getLogger(__name__)

# Yahoo Finance symbol grammar: optional caret for indices, then alphanumerics
# plus the separators used by class shares (BRK-B), exchange suffixes
# (RELIANCE.NS), crypto pairs (BTC-USD) and FX (EURUSD=X).
_SYMBOL_RE = re.compile(r"^\^?[A-Z0-9][A-Z0-9.\-=]{0,19}$")

_HEADER = """\
# Barbell Scanner watchlist. Paste your tickers below, in whatever shape you
# have them -- one per line, comma separated, or several to a line all work:
#
#     AAPL, MSFT, NVDA
#     GOOGL;AMZN
#     tsla meta
#
# Case does not matter and blank lines are fine. Text after # is a comment.
# If a line contains anything that is not a valid symbol, the WHOLE line is
# skipped and logged -- so check the run log after a bulk paste.
#
# Symbol format follows Yahoo Finance:
#   US equity / ETF   AAPL, SPY
#   Index             ^GSPC, ^NSEI
#   India (NSE/BSE)   RELIANCE.NS, TCS.NS, 500325.BO
#   Crypto            BTC-USD, ETH-USD
#   FX                EURUSD=X
#   Class shares      BRK-B
#
# This file is MACHINE-MANAGED: the Discord sync job rewrites it on !add and
# !remove, which normalises it (uppercase, deduplicated, sorted) and drops any
# comments you added below this header.
"""

DEFAULT_SETTINGS: dict = {
    "timeframes": ["daily", "weekly"],
    "cooldown_days": 14,
    "heartbeat": True,
    "report_problems": True,
    "batch_size": 25,
}


def normalise(symbol: str) -> str:
    """Canonicalise a user-supplied symbol. Returns '' if it is not valid."""
    candidate = symbol.strip().upper().lstrip("$")
    return candidate if _SYMBOL_RE.match(candidate) else ""


def load_tickers(path: Path = TICKERS_FILE) -> list[str]:
    """Read the watchlist, ignoring comments and blank lines.

    Deliberately forgiving about layout so a list pasted from anywhere -- a
    broker export, a spreadsheet column, a chat message -- just works. Symbols
    may be separated by newlines, commas, semicolons, tabs or spaces, in any
    mixture. Anything that cannot be parsed is WARNED about rather than
    silently dropped: a typo that quietly removes a position from the scan is
    far more expensive than a noisy log line.
    """
    if not path.exists():
        log.warning("watchlist %s does not exist", path)
        return []

    seen: set[str] = set()
    out: list[str] = []

    for lineno, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue

        tokens = [t for t in re.split(r"[,;\s]+", line) if t]
        parsed = [(t, normalise(t)) for t in tokens]
        bad = [t for t, symbol in parsed if not symbol]

        # A line is accepted only in full. Splitting on whitespace makes pasted
        # lists work, but it also means a line of prose fragments into tokens
        # that happen to be valid symbol shapes -- "not a ticker" yields NOT
        # and A, and A is a real listing (Agilent). So if any token on a line
        # fails to parse, the whole line is treated as suspect and reported
        # with everything it would have contributed, rather than half-ingested.
        if bad:
            taken = [s for _, s in parsed if s]
            log.warning(
                "watchlist line %d ignored -- unparseable %s%s. Line: %r",
                lineno,
                ", ".join(repr(t) for t in bad),
                f"; would have added {', '.join(taken)}" if taken else "",
                line,
            )
            continue

        for _, symbol in parsed:
            if symbol not in seen:
                seen.add(symbol)
                out.append(symbol)

    return out


def save_tickers(tickers: list[str], path: Path = TICKERS_FILE) -> None:
    """Rewrite the watchlist in canonical form (unique, sorted, uppercase)."""
    body = "\n".join(sorted(set(tickers)))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"{_HEADER}\n{body}\n", encoding="utf-8")


def load_settings(path: Path = SETTINGS_FILE) -> dict:
    """Read settings.yml, backfilling any key the user omitted."""
    settings = dict(DEFAULT_SETTINGS)
    if path.exists():
        loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if isinstance(loaded, dict):
            settings.update({k: v for k, v in loaded.items() if v is not None})
    return settings
