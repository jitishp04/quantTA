"""Market data acquisition via yfinance.

Downloads are batched (one HTTP round trip per chunk of tickers) rather than
looped per symbol, and each timeframe is trimmed to its exact spec'd candle
count so the analytical window is deterministic run to run.
"""

from __future__ import annotations

import datetime as dt
import logging

import pandas as pd
import yfinance as yf

from .config import TIMEFRAMES

log = logging.getLogger(__name__)

_OHLCV = ["Open", "High", "Low", "Close", "Volume"]


def _start_date(fetch_years: float) -> str:
    """Calendar start date giving comfortable headroom over the window."""
    start = dt.date.today() - dt.timedelta(days=int(fetch_years * 365.25))
    return start.isoformat()


def drop_forming_week(
    frame: pd.DataFrame,
    today: dt.date | None = None,
) -> tuple[pd.DataFrame, bool]:
    """Remove the final weekly bar if its week has not finished.

    yfinance stamps a weekly bar with the Monday that starts it and keeps
    updating that bar until the week ends -- so mid-week it is a running
    aggregate of the days elapsed so far, not a closed candle. Its close simply
    equals the latest daily close. Evaluating it means a weekly signal can
    appear on Wednesday and evaporate by Friday, which is exactly the whipsaw
    the 200-period/2.5-sigma setup exists to filter out.

    Completeness rule: a bar stamped Monday M is closed once ``today >= M + 7``,
    i.e. the following week has begun. This is deliberately conservative and
    asset-agnostic -- it is correct for a five-session equity week and for a
    seven-day crypto week without needing to know which one it is looking at.
    The cost is that an equity weekly signal confirms on Monday's run rather
    than Friday's; irrelevant for a signal held over months, and it buys the
    guarantee that a fired weekly alert is final and never retracted.

    Returns
    -------
    (frame, dropped)
    """
    if frame.empty:
        return frame, False
    today = today or dt.date.today()
    if (today - frame.index[-1].date()).days < 7:
        return frame.iloc[:-1], True
    return frame, False


def trades_weekends(frame: pd.DataFrame, lookback: int = 120) -> bool:
    """True if the instrument prints Saturday/Sunday bars (crypto, some FX).

    Derived from the series itself rather than guessed from the symbol, so a
    ``-USD`` suffix convention is never relied on.
    """
    return any(ts.weekday() >= 5 for ts in frame.tail(lookback).index)


def drop_forming_day(
    frame: pd.DataFrame,
    today: dt.date | None = None,
) -> tuple[pd.DataFrame, bool]:
    """Remove the final daily bar if that trading day has not closed yet.

    For an exchange-traded instrument the scan runs after the cash close, so
    the bar stamped with today's date is final and is kept. A 24/7 instrument
    has no session close -- its daily candle rolls at 00:00 UTC, which is
    hours after the scan runs, so the bar stamped today is still forming and
    would let a crypto signal retract overnight.
    """
    if frame.empty:
        return frame, False
    today = today or dt.datetime.now(dt.timezone.utc).date()
    if trades_weekends(frame) and frame.index[-1].date() >= today:
        return frame.iloc[:-1], True
    return frame, False


def _extract(raw: pd.DataFrame, ticker: str, single: bool) -> pd.DataFrame | None:
    """Pull one ticker's OHLCV frame out of a yfinance download result.

    yfinance returns flat columns for a single symbol and a (ticker, field)
    MultiIndex when `group_by="ticker"` is used with a list, so both shapes
    have to be handled.
    """
    if raw is None or raw.empty:
        return None

    if isinstance(raw.columns, pd.MultiIndex):
        if ticker not in raw.columns.get_level_values(0):
            return None
        frame = raw[ticker]
    elif single:
        frame = raw
    else:
        return None

    frame = frame.dropna(how="all")
    missing = [c for c in _OHLCV if c not in frame.columns]
    if missing or frame.empty:
        return None

    # A row with no close is unusable; forward-filled OHLC would fabricate bars.
    return frame[_OHLCV].dropna(subset=["Close"])


def fetch(
    tickers: list[str],
    timeframe: str,
    batch_size: int = 25,
    years: float | None = None,
    trim: bool = True,
) -> tuple[dict[str, pd.DataFrame], dict[str, str]]:
    """Download and window price history for every ticker on one timeframe.

    Parameters
    ----------
    years, trim
        Overrides for the study tools. The live scanner uses the defaults,
        which pin the analytical window to exactly the spec'd candle count.
        `backtest.py` needs decades of history to accumulate enough 2.5-sigma
        events to say anything, so it passes `years=50, trim=False`.

        This does not change any indicator value. SMA, EMA, RSI and the bands
        are all trailing, so the reading on a given date is the same whether it
        was computed over a 3-year window or a 30-year one -- the recursive
        smoothers differ only by the decayed influence of their seed, which is
        ~1e-14 by 756 bars. The window length governs how much history you can
        study, not what the indicators say.

    Returns
    -------
    (frames, problems)
        `frames` maps ticker -> OHLCV DataFrame. `problems` maps ticker -> a
        human-readable failure reason for anything that could not be scanned.
    """
    spec = TIMEFRAMES[timeframe]
    start = _start_date(years if years is not None else spec["fetch_years"])

    frames: dict[str, pd.DataFrame] = {}
    problems: dict[str, str] = {}

    for i in range(0, len(tickers), batch_size):
        chunk = tickers[i : i + batch_size]
        try:
            raw = yf.download(
                tickers=chunk,
                start=start,
                interval=spec["interval"],
                auto_adjust=True,      # split- and dividend-adjusted series
                group_by="ticker",
                threads=True,
                progress=False,
                actions=False,
            )
        except Exception as exc:  # network / upstream schema failures
            log.warning("batch download failed for %s: %s", chunk, exc)
            for ticker in chunk:
                problems[ticker] = f"download error: {exc}"
            continue

        for ticker in chunk:
            frame = _extract(raw, ticker, single=len(chunk) == 1)
            if frame is None:
                problems[ticker] = "no data returned (delisted or bad symbol?)"
                continue

            # Discard the in-progress candle BEFORE windowing, so the scan
            # still gets a full count of completed bars. Signals are only ever
            # evaluated on closed candles, so an alert is never retracted.
            if timeframe == "weekly":
                frame, _ = drop_forming_week(frame)
            else:
                frame, _ = drop_forming_day(frame)

            windowed = frame.tail(spec["window"]) if trim else frame
            if len(windowed) < spec["min_bars"]:
                problems[ticker] = (
                    f"insufficient {timeframe} history: {len(windowed)} bars, "
                    f"need {spec['min_bars']}"
                )
                continue

            frames[ticker] = windowed

    return frames, problems


def is_valid_symbol(ticker: str) -> bool:
    """Cheap existence probe used by the Discord !add handler."""
    try:
        probe = yf.Ticker(ticker).history(period="1mo", auto_adjust=True)
        return not probe.empty
    except Exception:
        return False
