"""Signal evaluation -- statistical extreme AND momentum extreme, together.

Trigger definitions (both conditions must hold on the same candle):

  CAPITULATION  Close < BBL_200_2.5  AND  RSI_21 < 30   -> accumulate / buy
  EUPHORIA      Close > BBU_200_2.5  AND  RSI_21 > 70   -> trim / take profit

The band test is evaluated as a vectorised boolean series across the whole
window so that `streak` (how many consecutive candles the condition has held)
comes for free, then only the final candle is reported.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from .config import (
    BBL_COL,
    BBM_COL,
    BBU_COL,
    EMA_COL,
    RSI_COL,
    RSI_OVERBOUGHT,
    RSI_OVERSOLD,
    SMA_COL,
    TIMEFRAMES,
)

CAPITULATION = "CAPITULATION"
EUPHORIA = "EUPHORIA"
NEUTRAL = "NEUTRAL"


@dataclass
class Snapshot:
    """The scan result for one ticker on one timeframe."""

    ticker: str
    timeframe: str
    signal: str
    bar_date: str
    close: float
    rsi: float
    bbl: float
    bbm: float
    bbu: float
    sma50: float
    ema200: float
    percent_b: float          # position within the bands; <0 below, >1 above
    band_distance_pct: float  # % beyond the breached band (0 when neutral)
    streak: int               # consecutive candles the condition has held
    context: list[str] = field(default_factory=list)

    @property
    def key(self) -> str:
        return f"{self.ticker}|{self.timeframe}"

    @property
    def label(self) -> str:
        return TIMEFRAMES[self.timeframe]["label"]


def _streak(flags: pd.Series) -> int:
    """Count consecutive True values ending at the final element."""
    values = flags.to_numpy()
    if not values.size or not values[-1]:
        return 0
    # Distance from the end back to the most recent False.
    inactive = np.flatnonzero(~values)
    return int(values.size - inactive[-1] - 1) if inactive.size else int(values.size)


def evaluate(df: pd.DataFrame, ticker: str, timeframe: str) -> Snapshot | None:
    """Reduce an indicator-enriched frame to a single Snapshot.

    Returns None if the most recent candle has not yet warmed up the 200-period
    indicators (any required value is NaN).
    """
    close = df["Close"]
    rsi = df[RSI_COL]

    capitulation = (close < df[BBL_COL]) & (rsi < RSI_OVERSOLD)
    euphoria = (close > df[BBU_COL]) & (rsi > RSI_OVERBOUGHT)

    last = df.iloc[-1]
    required = [last["Close"], last[RSI_COL], last[BBL_COL], last[BBM_COL], last[BBU_COL]]
    if any(pd.isna(v) for v in required):
        return None

    if bool(capitulation.iloc[-1]):
        signal, streak = CAPITULATION, _streak(capitulation)
    elif bool(euphoria.iloc[-1]):
        signal, streak = EUPHORIA, _streak(euphoria)
    else:
        signal, streak = NEUTRAL, 0

    price = float(last["Close"])
    bbl, bbm, bbu = float(last[BBL_COL]), float(last[BBM_COL]), float(last[BBU_COL])
    width = bbu - bbl

    if signal == CAPITULATION:
        distance = (price - bbl) / bbl * 100.0
    elif signal == EUPHORIA:
        distance = (price - bbu) / bbu * 100.0
    else:
        distance = 0.0

    sma50 = float(last[SMA_COL]) if pd.notna(last[SMA_COL]) else float("nan")
    ema200 = float(last[EMA_COL]) if pd.notna(last[EMA_COL]) else float("nan")

    context: list[str] = []
    if not np.isnan(sma50):
        context.append(f"{'above' if price >= sma50 else 'below'} 50SMA")
    if not np.isnan(ema200):
        context.append(f"{'above' if price >= ema200 else 'below'} 200EMA")

    return Snapshot(
        ticker=ticker,
        timeframe=timeframe,
        signal=signal,
        bar_date=df.index[-1].strftime("%Y-%m-%d"),
        close=price,
        rsi=float(last[RSI_COL]),
        bbl=bbl,
        bbm=bbm,
        bbu=bbu,
        sma50=sma50,
        ema200=ema200,
        percent_b=(price - bbl) / width if width else float("nan"),
        band_distance_pct=distance,
        streak=streak,
        context=context,
    )
