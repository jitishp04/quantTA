"""Indicator layer -- fully vectorised pandas_ta computation.

Every series is produced in one pass over the frame. There are no row-wise
loops anywhere in this module, and none should ever be introduced: the scan
runs the same code path over hundreds of symbols on a shared CI runner.

API NOTE (pandas_ta 0.4.x)
-------------------------
The Bollinger call is deliberately verbose because 0.4.x changed the signature
to ``bbands(close, length, lower_std, upper_std, ddof, mamode, talib, offset)``.
The pre-0.4 keyword ``std=`` no longer exists; passing it lands in **kwargs and
is SILENTLY DISCARDED, leaving you with the 2.0 default -- i.e. 2.0-sigma bands
masquerading as 2.5-sigma ones, with no error and no warning. Both multipliers
are therefore passed explicitly, and a self-check below asserts the columns we
asked for actually came back.
"""

from __future__ import annotations

import pandas as pd
import pandas_ta as ta

from .config import (
    BB_LENGTH,
    BB_STD,
    BBL_COL,
    BBM_COL,
    BBU_COL,
    EMA_COL,
    EMA_LENGTH,
    RSI_COL,
    RSI_LENGTH,
    SMA_COL,
    SMA_LENGTH,
)

REQUIRED_COLS = (SMA_COL, EMA_COL, RSI_COL, BBL_COL, BBM_COL, BBU_COL)

# What pandas_ta 0.4.x actually names the three band columns, mapped onto the
# canonical single-sigma names used everywhere else in this project.
_BB_RENAME = {
    f"BBL_{BB_LENGTH}_{BB_STD}_{BB_STD}": BBL_COL,
    f"BBM_{BB_LENGTH}_{BB_STD}_{BB_STD}": BBM_COL,
    f"BBU_{BB_LENGTH}_{BB_STD}_{BB_STD}": BBU_COL,
}


def compute(df: pd.DataFrame) -> pd.DataFrame:
    """Append the full indicator stack to an OHLCV frame.

    Produces, using the fixed barbell parameters:
      SMA_50, EMA_200, RSI_21, BBL_200_2.5, BBM_200_2.5, BBU_200_2.5

    Raises
    ------
    ValueError
        If pandas_ta returns nothing, or returns bands at a sigma other than
        the one requested -- which would quietly invalidate every signal.
    """
    close = df["Close"].astype("float64")
    out = df.copy()

    out[SMA_COL] = ta.sma(close, length=SMA_LENGTH, talib=False)
    out[EMA_COL] = ta.ema(close, length=EMA_LENGTH, talib=False)
    out[RSI_COL] = ta.rsi(close, length=RSI_LENGTH, talib=False)

    # talib=False + ddof=0 pins the population-sigma pandas path, so results do
    # not shift depending on whether TA-Lib happens to be present on the runner.
    bbands = ta.bbands(
        close,
        length=BB_LENGTH,
        lower_std=BB_STD,
        upper_std=BB_STD,
        ddof=0,
        mamode="sma",
        talib=False,
    )
    if bbands is None or bbands.empty:
        raise ValueError(f"bollinger bands unavailable ({len(df)} bars)")

    missing_native = [c for c in _BB_RENAME if c not in bbands.columns]
    if missing_native:
        raise ValueError(
            f"pandas_ta returned {list(bbands.columns)}; expected "
            f"{list(_BB_RENAME)} -- the BB sigma parameter did not take effect"
        )

    bands = bbands[list(_BB_RENAME)].rename(columns=_BB_RENAME)
    out = pd.concat([out, bands], axis=1)

    missing = [c for c in REQUIRED_COLS if c not in out.columns]
    if missing:
        raise ValueError(f"indicator columns missing: {missing}")

    return out
