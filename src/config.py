"""Immutable mathematical constants for the barbell scanner.

These parameters define the analytical environment and are intentionally NOT
exposed to config/settings.yml. The whole point of the setup is that the
lookback windows and indicator lengths stay static across every scan, so that
a signal fired in 2026 means exactly what a signal fired in 2024 meant.

Do not "tune" these.
"""

from __future__ import annotations

import os
from pathlib import Path

# --------------------------------------------------------------------------
# Paths
# --------------------------------------------------------------------------
ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT / "config"
STATE_DIR = ROOT / "state"

TICKERS_FILE = CONFIG_DIR / "tickers.txt"
SETTINGS_FILE = CONFIG_DIR / "settings.yml"
ALERT_STATE_FILE = STATE_DIR / "alerts.json"

# --------------------------------------------------------------------------
# Indicator lengths -- identical across both chart environments
# --------------------------------------------------------------------------
SMA_LENGTH = 50       # core trend anchor (fast)
EMA_LENGTH = 200      # core trend anchor (slow)
RSI_LENGTH = 21       # momentum filter
BB_LENGTH = 200       # statistical volatility baseline
BB_STD = 2.5          # standard deviations

# Canonical column names. SMA/EMA/RSI match pandas_ta's own output naming.
#
# The Bollinger names are OURS, not the library's. pandas_ta 0.4.x supports
# asymmetric bands and therefore emits a two-multiplier suffix
# ("BBL_200_2.5_2.5"). We rename to the single-sigma form in indicators.py so
# the codebase speaks the spec's language, and so a future upstream naming
# change is absorbed in exactly one place.
SMA_COL = f"SMA_{SMA_LENGTH}"
EMA_COL = f"EMA_{EMA_LENGTH}"
RSI_COL = f"RSI_{RSI_LENGTH}"
BBL_COL = f"BBL_{BB_LENGTH}_{BB_STD}"   # -> BBL_200_2.5
BBM_COL = f"BBM_{BB_LENGTH}_{BB_STD}"   # -> BBM_200_2.5
BBU_COL = f"BBU_{BB_LENGTH}_{BB_STD}"   # -> BBU_200_2.5

# --------------------------------------------------------------------------
# Trigger thresholds
# --------------------------------------------------------------------------
RSI_OVERSOLD = 30.0    # capitulation gate
RSI_OVERBOUGHT = 70.0  # euphoria gate

# --------------------------------------------------------------------------
# Lookback windows
#
# Each timeframe is trimmed to an EXACT candle count so the analytical window
# is deterministic regardless of holidays or how many bars Yahoo returns. We
# request a calendar buffer well beyond the window, then tail() to the spec.
#
# `min_bars` is the hard floor for a usable read: BB(200) and EMA(200) both
# need 200 completed periods before they emit a non-NaN value.
# --------------------------------------------------------------------------
def load_dotenv(path: Path | None = None) -> None:
    """Load KEY=VALUE pairs from a local .env into the environment.

    Deliberately dependency-free -- this is the only thing python-dotenv would
    have been pulled in for, and the format here is a handful of lines.

    Uses `setdefault`, so a real environment variable ALWAYS wins over the
    file. That ordering matters: on GitHub Actions the secrets arrive as real
    environment variables, and a stale committed .env must never be able to
    shadow them. (`.env` is gitignored, but the guarantee should not depend on
    that holding.)
    """
    path = path or (ROOT / ".env")
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        line = line.removeprefix("export ").strip()
        key, separator, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if not key or not separator:
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        if value:
            os.environ.setdefault(key, value)


TIMEFRAMES: dict[str, dict] = {
    "daily": {
        "label": "DAILY",
        "interval": "1d",
        "window": 756,          # ~3 years of trading days
        "fetch_years": 4.0,     # calendar buffer to guarantee 756 bars exist
        "min_bars": BB_LENGTH,
    },
    "weekly": {
        "label": "WEEKLY",
        "interval": "1wk",
        "window": 260,          # ~5 years of weekly candles
        "fetch_years": 6.5,     # calendar buffer to guarantee 260 bars exist
        "min_bars": BB_LENGTH,
    },
}
