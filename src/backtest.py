"""Layer 1 -- conditional forward-return study.

Answers the only question that matters before trusting a signal: *when this
exact condition fired historically, what happened next?*

Three things separate this from the usual backtest that flatters itself:

1. **Edge-triggered events.** A capitulation that persists for 36 candles is
   ONE event, not 36. Counting every bar in a streak inflates the sample and
   makes a single 2008-shaped episode look like statistical significance.

2. **A baseline.** A 70% win rate at six months is not an edge -- equities are
   up over six months roughly that often regardless of what you did. Every
   statistic here is reported against the unconditional forward return over
   the same instrument and the same history. The edge is the *difference*.

3. **Max adverse excursion.** Mean forward return says the trade worked. MAE
   says how far underwater you sat first. For a capitulation entry that is the
   number that decides position size, and it is the number most backtests omit.

Every computation is vectorised via `sliding_window_view`; there is no loop
over bars anywhere in this module.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd
from numpy.lib.stride_tricks import sliding_window_view

from .config import BBL_COL, BBU_COL, RSI_COL, RSI_OVERBOUGHT, RSI_OVERSOLD
from .signals import CAPITULATION, EUPHORIA

# Forward horizons per timeframe, in candles of that timeframe.
HORIZONS: dict[str, tuple[int, ...]] = {
    "daily": (21, 63, 126, 252),    # ~1m, 3m, 6m, 1y
    "weekly": (4, 13, 26, 52),      # ~1m, 3m, 6m, 1y
}

N_BOOTSTRAP = 10_000
BOOTSTRAP_SEED = 20260903

# Minimum separation between two events, in candles. A condition that blinks
# false for one bar and re-fires is the SAME episode, not a new observation:
# SPY 2011-08-08 and 2011-08-10 are one August-2011 selloff, and counting both
# double-weights it while making the bootstrap look more confident than the
# evidence supports. Roughly one month on each timeframe.
MIN_EVENT_GAP = {"daily": 21, "weekly": 4}


@dataclass
class HorizonStats:
    """Forward-return statistics at one horizon, conditional vs unconditional."""

    horizon: int
    n_events: int
    win_rate: float
    mean_return: float
    median_return: float
    worst_return: float
    best_return: float
    median_mae: float          # typical max drawdown before the horizon
    worst_mae: float
    median_mfe: float
    baseline_n: int
    baseline_win_rate: float
    baseline_mean: float
    baseline_median: float
    p_value: float             # bootstrap, vs the unconditional distribution

    @property
    def edge_mean(self) -> float:
        """Excess mean return over simply holding the instrument."""
        return self.mean_return - self.baseline_mean

    @property
    def edge_win_rate(self) -> float:
        return self.win_rate - self.baseline_win_rate

    @property
    def significant(self) -> bool:
        """Conventional 5% threshold AND enough events to mean anything."""
        return self.p_value < 0.05 and self.n_events >= 10


@dataclass
class BacktestResult:
    ticker: str
    timeframe: str
    signal: str
    history_start: str
    history_end: str
    total_bars: int
    event_dates: list[str] = field(default_factory=list)
    stats: list[HorizonStats] = field(default_factory=list)
    note: str = ""

    @property
    def n_events(self) -> int:
        return len(self.event_dates)


def signal_mask(df: pd.DataFrame, signal: str) -> pd.Series:
    """The raw per-bar condition, identical to the live scanner's test."""
    close = df["Close"]
    if signal == CAPITULATION:
        return (close < df[BBL_COL]) & (df[RSI_COL] < RSI_OVERSOLD)
    if signal == EUPHORIA:
        return (close > df[BBU_COL]) & (df[RSI_COL] > RSI_OVERBOUGHT)
    raise ValueError(f"unknown signal {signal!r}")


def event_positions(mask: pd.Series, min_gap: int = 0) -> np.ndarray:
    """Integer positions of the FIRST bar of each distinct episode.

    Edge triggering keeps a long streak from being counted as many independent
    observations. `min_gap` then debounces re-triggers: an episode that lapses
    for a bar or two and resumes is still one episode.
    """
    values = mask.to_numpy(dtype=bool)
    if values.size == 0:
        return np.array([], dtype=int)

    previous = np.concatenate(([False], values[:-1]))
    raw = np.flatnonzero(values & ~previous)
    if min_gap <= 0 or raw.size == 0:
        return raw

    # Iterates over episodes (a handful over decades), never over bars.
    kept = [int(raw[0])]
    for position in raw[1:]:
        if position - kept[-1] >= min_gap:
            kept.append(int(position))
    return np.array(kept, dtype=int)


def _forward_matrix(close: np.ndarray, horizon: int) -> np.ndarray | None:
    """Rolling view where row i is close[i .. i+horizon] inclusive."""
    if close.size <= horizon:
        return None
    return sliding_window_view(close, horizon + 1)


def _bootstrap_p(
    baseline: np.ndarray,
    observed_mean: float,
    n_events: int,
    direction: str,
) -> float:
    """Probability of drawing this mean from the unconditional distribution.

    A permutation-flavoured test: sample `n_events` forward returns at random
    from all warmed-up bars, many times, and ask how often chance alone beats
    what the signal produced.

    Caveat carried into the report: overlapping forward windows are serially
    correlated, and events cluster inside single drawdowns, so the effective
    sample is smaller than `n_events` suggests. This p-value is optimistic.
    Treat it as a smell test, not a proof.
    """
    if baseline.size == 0 or n_events == 0:
        return float("nan")
    rng = np.random.default_rng(BOOTSTRAP_SEED)
    draws = rng.choice(baseline, size=(N_BOOTSTRAP, n_events), replace=True)
    means = draws.mean(axis=1)
    if direction == "upper":
        return float((means >= observed_mean).mean())
    return float((means <= observed_mean).mean())


def run(
    df: pd.DataFrame,
    ticker: str,
    timeframe: str,
    signal: str,
    horizons: tuple[int, ...] | None = None,
) -> BacktestResult:
    """Study every historical occurrence of `signal` in an enriched frame.

    `df` must already carry the indicator stack (see indicators.compute) and
    should span as much history as available -- the whole point is to gather
    enough 2.5-sigma events to say something.
    """
    horizons = horizons or HORIZONS[timeframe]
    close = df["Close"].to_numpy(dtype="float64")

    mask = signal_mask(df, signal)
    gap = MIN_EVENT_GAP[timeframe]
    events = event_positions(mask, min_gap=gap)
    merged = event_positions(mask).size - events.size

    # The comparison universe: every bar where the indicators are warmed up.
    # Comparing against unwarmed bars would mix in a different regime.
    warm = df[BBL_COL].notna().to_numpy(dtype=bool)

    result = BacktestResult(
        ticker=ticker,
        timeframe=timeframe,
        signal=signal,
        history_start=df.index[0].strftime("%Y-%m-%d"),
        history_end=df.index[-1].strftime("%Y-%m-%d"),
        total_bars=len(df),
        event_dates=[df.index[i].strftime("%Y-%m-%d") for i in events],
    )

    if events.size == 0:
        result.note = "no historical occurrences in the available history"
        return result

    direction = "upper" if signal == CAPITULATION else "lower"

    for horizon in horizons:
        windows = _forward_matrix(close, horizon)
        if windows is None:
            continue

        entry = windows[:, 0]
        path = windows[:, 1:]                      # bars strictly after entry
        forward = windows[:, -1] / entry - 1.0
        mae = path.min(axis=1) / entry - 1.0       # worst point before horizon
        mfe = path.max(axis=1) / entry - 1.0

        # Only events with a COMPLETE forward window are usable. Truncating a
        # window and treating the partial result as a full-horizon outcome is
        # the classic way to smuggle a bull-market bias into a backtest.
        usable = events[events < windows.shape[0]]
        if usable.size == 0:
            continue

        warm_positions = np.flatnonzero(warm[: windows.shape[0]])
        baseline = forward[warm_positions]

        conditional = forward[usable]
        result.stats.append(
            HorizonStats(
                horizon=horizon,
                n_events=int(usable.size),
                win_rate=float((conditional > 0).mean()),
                mean_return=float(conditional.mean()),
                median_return=float(np.median(conditional)),
                worst_return=float(conditional.min()),
                best_return=float(conditional.max()),
                median_mae=float(np.median(mae[usable])),
                worst_mae=float(mae[usable].min()),
                median_mfe=float(np.median(mfe[usable])),
                baseline_n=int(baseline.size),
                baseline_win_rate=float((baseline > 0).mean()) if baseline.size else float("nan"),
                baseline_mean=float(baseline.mean()) if baseline.size else float("nan"),
                baseline_median=float(np.median(baseline)) if baseline.size else float("nan"),
                p_value=_bootstrap_p(baseline, float(conditional.mean()),
                                     int(usable.size), direction),
            )
        )

    notes: list[str] = []
    if merged:
        notes.append(
            f"{merged} re-trigger(s) merged into a prior episode "
            f"(<{gap} candles apart)"
        )
    if result.stats and result.stats[0].n_events < 10:
        notes.append(
            f"only {result.stats[0].n_events} complete events -- "
            "descriptive at best, not significant"
        )
    result.note = "; ".join(notes)
    return result
