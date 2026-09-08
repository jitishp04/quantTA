"""Watchlist and settings I/O.

The ticker list lives in a plain text file rather than inside settings.yml so
that editing it is a one-line diff with no YAML syntax to get wrong, and so a
list pasted from a broker export or a spreadsheet column just works.

This file is hand-managed: edit config/tickers.txt and commit. Nothing in the
scanner writes to it.
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


def load_settings(path: Path = SETTINGS_FILE) -> dict:
    """Read settings.yml, backfilling any key the user omitted."""
    settings = dict(DEFAULT_SETTINGS)
    if path.exists():
        loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if isinstance(loaded, dict):
            settings.update({k: v for k, v in loaded.items() if v is not None})
    return settings
