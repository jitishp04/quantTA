"""Self-checks for the barbell scanner.

Run with plain Python (no pytest needed):

    python tests/test_pipeline.py

The important test here is `test_bollinger_sigma_is_actually_2_5`. pandas_ta
accepts unknown keywords into **kwargs without raising, so an API drift can
leave the scanner computing 2.0-sigma bands while every log line still says
2.5. That silently loosens every trigger, so the sigma is verified against a
hand-rolled numpy computation rather than trusted.
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import (  # noqa: E402
    BB_LENGTH,
    BB_STD,
    BBL_COL,
    BBM_COL,
    BBU_COL,
    EMA_LENGTH,
    RSI_COL,
    RSI_LENGTH,
    SMA_COL,
    SMA_LENGTH,
)
from src.data import drop_forming_day, drop_forming_week, trades_weekends  # noqa: E402
from src.indicators import compute  # noqa: E402
from src.notify import format_alerts  # noqa: E402
from src.signals import CAPITULATION, EUPHORIA, NEUTRAL, _streak, evaluate  # noqa: E402
from src.state import AlertState  # noqa: E402
from src.watchlist import normalise  # noqa: E402


def _frame(closes: np.ndarray) -> pd.DataFrame:
    """Wrap a close series in a minimal OHLCV frame on a business-day index."""
    index = pd.date_range("2021-01-04", periods=len(closes), freq="B")
    return pd.DataFrame(
        {
            "Open": closes,
            "High": closes * 1.005,
            "Low": closes * 0.995,
            "Close": closes,
            "Volume": np.full(len(closes), 1_000_000.0),
        },
        index=index,
    )


def _random_walk(n: int, seed: int = 42, drift: float = 0.0002) -> np.ndarray:
    rng = np.random.default_rng(seed)
    return 100 * np.exp(np.cumsum(rng.normal(drift, 0.011, n)))


def _shock(n: int = 756, tail: int = 40, factor: float = 0.55, seed: int = 42) -> np.ndarray:
    """A calm walk followed by a sharp directional move in the final `tail` bars."""
    base = _random_walk(n, seed)
    ramp = np.linspace(1.0, factor, tail)
    out = base.copy()
    out[-tail:] = out[-tail] * ramp
    return out


# ---------------------------------------------------------------------------
# Indicator correctness
# ---------------------------------------------------------------------------

def test_bollinger_sigma_is_actually_2_5() -> None:
    """Bands must sit at exactly 2.5 population sigma from a 200-period SMA."""
    df = compute(_frame(_random_walk(756)))
    close = df["Close"]

    expected_mid = close.rolling(BB_LENGTH).mean()
    expected_sd = close.rolling(BB_LENGTH).std(ddof=0)
    expected_lower = expected_mid - BB_STD * expected_sd
    expected_upper = expected_mid + BB_STD * expected_sd

    for name, actual, expected in (
        ("BBM", df[BBM_COL], expected_mid),
        ("BBL", df[BBL_COL], expected_lower),
        ("BBU", df[BBU_COL], expected_upper),
    ):
        valid = expected.notna()
        drift = (actual[valid] - expected[valid]).abs().max()
        assert drift < 1e-8, f"{name} deviates from 2.5-sigma reference by {drift}"

    # Guard against the specific silent-failure mode: 2.0-sigma bands.
    sigma_20_lower = expected_mid - 2.0 * expected_sd
    assert not np.allclose(
        df[BBL_COL].dropna(), sigma_20_lower.dropna()
    ), "bands collapsed to the 2.0 default -- the sigma argument did not apply"
    print("  ok  bollinger bands are 2.5 sigma, ddof=0, SMA-centred")


def test_rsi_matches_wilder() -> None:
    """RSI_21 must be Wilder-smoothed, not a simple average of gains/losses."""
    df = compute(_frame(_random_walk(756)))
    delta = df["Close"].diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    alpha = 1.0 / RSI_LENGTH
    rs = gain.ewm(alpha=alpha, adjust=False).mean() / loss.ewm(alpha=alpha, adjust=False).mean()
    reference = 100 - 100 / (1 + rs)

    # Recursive smoothing converges; compare where the seed no longer matters.
    drift = (df[RSI_COL] - reference).abs().iloc[200:].max()
    assert drift < 0.05, f"RSI deviates from Wilder reference by {drift}"
    print(f"  ok  RSI_21 matches Wilder smoothing (max drift {drift:.2e})")


def test_indicator_columns_and_warmup() -> None:
    df = compute(_frame(_random_walk(756)))
    for col in (SMA_COL, f"EMA_{EMA_LENGTH}", RSI_COL, BBL_COL, BBM_COL, BBU_COL):
        assert col in df.columns, f"missing {col}"
    assert df[SMA_COL].isna().sum() == SMA_LENGTH - 1
    assert df[BBL_COL].isna().sum() == BB_LENGTH - 1
    assert df[BBL_COL].iloc[-1] < df[BBM_COL].iloc[-1] < df[BBU_COL].iloc[-1]
    print("  ok  indicator columns present, warmup and band ordering correct")


# ---------------------------------------------------------------------------
# Signal logic
# ---------------------------------------------------------------------------

def test_capitulation_fires() -> None:
    snap = evaluate(compute(_frame(_shock(factor=0.55))), "CRASH", "daily")
    assert snap is not None
    assert snap.signal == CAPITULATION, f"expected CAPITULATION, got {snap.signal}"
    assert snap.close < snap.bbl and snap.rsi < 30
    assert snap.percent_b < 0 and snap.band_distance_pct < 0 and snap.streak >= 1
    print(f"  ok  capitulation fires (rsi {snap.rsi:.1f}, %B {snap.percent_b:.3f}, "
          f"streak {snap.streak})")


def test_euphoria_fires() -> None:
    snap = evaluate(compute(_frame(_shock(factor=1.85))), "MELTUP", "daily")
    assert snap is not None
    assert snap.signal == EUPHORIA, f"expected EUPHORIA, got {snap.signal}"
    assert snap.close > snap.bbu and snap.rsi > 70
    assert snap.percent_b > 1 and snap.band_distance_pct > 0
    print(f"  ok  euphoria fires (rsi {snap.rsi:.1f}, %B {snap.percent_b:.3f}, "
          f"streak {snap.streak})")


def test_band_breach_without_rsi_stays_neutral() -> None:
    """The AND is the whole point: a band breach alone must not alert."""
    closes = _shock(factor=0.62)
    df = compute(_frame(closes))
    df.loc[df.index[-1], RSI_COL] = 45.0        # breach intact, momentum neutral
    snap = evaluate(df, "HALFSIGNAL", "daily")
    assert snap is not None and snap.signal == NEUTRAL, (
        f"band breach without RSI confirmation leaked through as {snap.signal}"
    )
    print("  ok  band breach without RSI confirmation stays NEUTRAL")


def test_rsi_extreme_without_band_stays_neutral() -> None:
    df = compute(_frame(_random_walk(756)))
    df.loc[df.index[-1], RSI_COL] = 12.0        # momentum extreme, price inside bands
    snap = evaluate(df, "RSIONLY", "daily")
    assert snap is not None and snap.signal == NEUTRAL
    print("  ok  RSI extreme without band breach stays NEUTRAL")


def test_forming_weekly_bar_is_dropped() -> None:
    """An unfinished weekly candle must never reach the signal evaluator.

    The bar is stamped with the Monday that starts it and keeps mutating until
    the week ends, so acting on it mid-week produces signals that retract.
    """
    import datetime as dt

    # Three weekly bars: 17 Aug, 24 Aug, 31 Aug (all Mondays).
    index = pd.to_datetime(["2026-08-17", "2026-08-24", "2026-08-31"])
    frame = pd.DataFrame({"Close": [100.0, 101.0, 102.0]}, index=index)

    cases = [
        ("2026-09-02", "Wednesday, 2 of 5 days in", True),
        ("2026-09-04", "Friday, equity week just closed", True),
        ("2026-09-06", "Sunday, crypto week closing", True),
        ("2026-09-07", "Monday, week fully elapsed", False),
        ("2026-09-14", "a week later", False),
    ]
    for iso, why, expect_dropped in cases:
        out, dropped = drop_forming_week(frame, dt.date.fromisoformat(iso))
        assert dropped == expect_dropped, f"{iso} ({why}): dropped={dropped}"
        assert len(out) == (2 if expect_dropped else 3)
        if expect_dropped:
            assert out.index[-1].date() == dt.date(2026, 8, 24)

    empty, dropped = drop_forming_week(pd.DataFrame({"Close": []}), dt.date(2026, 9, 2))
    assert empty.empty and dropped is False, "empty frame must not raise"
    print("  ok  forming weekly bar dropped until its week fully elapses")


def test_forming_daily_bar_dropped_only_for_24_7_assets() -> None:
    """An equity's post-close bar is final; a 24/7 asset's same-dated bar is not.

    The scan runs 21:30 UTC. A US session closed hours earlier, so the bar
    stamped today is complete and must be kept. Crypto rolls at 00:00 UTC, so
    the bar stamped today still has ~2.5 hours to run and must be dropped.
    """
    import datetime as dt

    # Weekdays only -> exchange-traded.
    equity = pd.DataFrame(
        {"Close": np.arange(60.0)},
        index=pd.date_range("2026-06-08", periods=60, freq="B"),
    )
    # Every calendar day -> 24/7.
    crypto = pd.DataFrame(
        {"Close": np.arange(60.0)},
        index=pd.date_range("2026-07-05", periods=60, freq="D"),
    )
    assert trades_weekends(equity) is False
    assert trades_weekends(crypto) is True

    today = equity.index[-1].date()
    kept, dropped = drop_forming_day(equity, today)
    assert dropped is False and len(kept) == 60, "equity post-close bar must be kept"

    today = crypto.index[-1].date()
    trimmed, dropped = drop_forming_day(crypto, today)
    assert dropped is True and len(trimmed) == 59, "crypto same-day bar must be dropped"

    # Next day: yesterday's crypto candle has closed and is now usable.
    settled, dropped = drop_forming_day(crypto, today + dt.timedelta(days=1))
    assert dropped is False and len(settled) == 60

    empty, dropped = drop_forming_day(pd.DataFrame({"Close": []}), dt.date(2026, 9, 2))
    assert empty.empty and dropped is False
    print("  ok  forming daily bar dropped for 24/7 assets, kept for exchanges")


def test_streak_counting() -> None:
    assert _streak(pd.Series([False, False, False])) == 0
    assert _streak(pd.Series([True, True, False])) == 0
    assert _streak(pd.Series([False, True, True])) == 2
    assert _streak(pd.Series([True, True, True])) == 3
    assert _streak(pd.Series([True, False, True])) == 1
    print("  ok  streak counting")


def test_insufficient_history_is_rejected() -> None:
    """Two distinct guards protect against acting on an unwarmed 200-period read.

    A frame shorter than BB(200) cannot produce bands at all, so compute()
    raises and scan.py records it as a problem. A frame that is long enough but
    still carries NaN on the final candle must yield no snapshot rather than a
    signal derived from a missing band.
    """
    try:
        compute(_frame(_random_walk(150)))
        raise AssertionError("compute() accepted a 150-bar frame")
    except ValueError:
        pass

    # Exactly BB_LENGTH bars is the documented floor and must work.
    edge = evaluate(compute(_frame(_random_walk(BB_LENGTH))), "EDGE", "daily")
    assert edge is not None, f"{BB_LENGTH} bars should be sufficient"

    # NaN on the final candle -> no snapshot.
    df = compute(_frame(_random_walk(756)))
    df.loc[df.index[-1], BBL_COL] = np.nan
    assert evaluate(df, "NANBAR", "daily") is None
    print(f"  ok  short frame raises, {BB_LENGTH}-bar floor works, NaN bar returns None")


# ---------------------------------------------------------------------------
# De-duplication state
# ---------------------------------------------------------------------------

def test_state_deduplicates(tmp: Path) -> None:
    state = AlertState(tmp / "alerts.json")
    snap = evaluate(compute(_frame(_shock(factor=0.55))), "CRASH", "daily")

    assert state.should_alert(snap, 14) is True, "first sighting must alert"
    state.record(snap)
    assert state.should_alert(snap, 14) is False, "same-day repeat must be silent"

    # Backdate beyond the cooldown -> one re-notification.
    state.data[snap.key]["date"] = "2000-01-01"
    assert state.should_alert(snap, 14) is True, "stale signal must re-notify"

    # cooldown_days=0 means edge-only: never re-notify while unchanged.
    state.record(snap)
    state.data[snap.key]["date"] = "2000-01-01"
    assert state.should_alert(snap, 0) is False, "cooldown 0 must be edge-only"

    # Returning to neutral re-arms the setup.
    snap.signal = NEUTRAL
    assert state.should_alert(snap, 14) is False
    assert snap.key not in state.data, "neutral must clear the record"

    state.save()
    assert (tmp / "alerts.json").exists()
    print("  ok  alert de-duplication, cooldown and re-arm")


# ---------------------------------------------------------------------------
# Rendering and input hygiene
# ---------------------------------------------------------------------------

def test_alert_rendering() -> None:
    """Payload shape, ordering, and Discord's embed limits."""
    from src.notify import (MAX_EMBED_DESCRIPTION, MAX_EMBEDS_PER_MESSAGE,
                            _split_payload)

    cap = evaluate(compute(_frame(_shock(factor=0.55))), "CRASH", "daily")
    eup = evaluate(compute(_frame(_shock(factor=1.85))), "MELTUP", "weekly")
    payload = format_alerts([eup, cap], {"BADSYM": "no data"})

    titles = [e["title"] for e in payload["embeds"]]
    assert any("CRASH" in t for t in titles) and any("MELTUP" in t for t in titles)
    crash = next(i for i, t in enumerate(titles) if "CRASH" in t)
    meltup = next(i for i, t in enumerate(titles) if "MELTUP" in t)
    assert crash < meltup, "capitulation must sort first"

    assert "BARBELL SCAN" in payload["content"]
    assert payload["embeds"][-1]["title"].startswith("⚠"), "problems appended last"

    for embed in payload["embeds"]:
        assert len(embed["description"]) <= MAX_EMBED_DESCRIPTION
        assert embed["description"].count("```") % 2 == 0, "unbalanced code fence"

    # Oversized batches must split rather than 400 on Discord's 10-embed cap.
    many = format_alerts([cap] * 26)
    messages = _split_payload(many)
    assert len(messages) >= 3, "26 embeds must split across messages"
    assert all(len(m["embeds"]) <= MAX_EMBEDS_PER_MESSAGE for m in messages)
    assert sum(len(m["embeds"]) for m in messages) == 26, "no embed lost in split"
    assert "content" in messages[0] and "content" not in messages[1], (
        "header belongs on the first message only"
    )
    print("  ok  embed payload, ordering, fence balance and 10-embed splitting")


def test_retry_uses_ticker_first_column_order() -> None:
    """The single-symbol retry path must request group_by='ticker'.

    yfinance orders columns as (Field, Ticker) unless group_by='ticker' is
    explicitly passed, even for a single symbol -- _extract only recognises
    (Ticker, Field). Omitting the kwarg makes every retry silently fail
    regardless of whether Yahoo actually has the data, which is exactly what
    shipped and reported SPY as unavailable when it was not. Asserted against
    the call signature rather than a live network call, so this stays fast and
    deterministic.
    """
    from unittest.mock import patch

    from src.data import _retry_single
    from src.config import TIMEFRAMES

    captured = {}

    def fake_download(**kwargs):
        captured.update(kwargs)
        return pd.DataFrame()   # empty is fine; only the call shape is checked

    with patch("src.data.yf.download", side_effect=fake_download):
        _retry_single("SPY", TIMEFRAMES["daily"], "2020-01-01")

    assert captured.get("group_by") == "ticker", (
        "retry omitted group_by='ticker' -- yfinance returns (Field, Ticker) "
        "column order instead, which _extract cannot parse, so every retry "
        "would silently fail even when Yahoo has the data"
    )
    print("  ok  single-symbol retry requests ticker-first column order")


def test_batch_omission_recovers_via_retry() -> None:
    """A symbol dropped from an otherwise-successful batch response must be
    recovered by re-requesting it alone, not reported as a problem.
    """
    from unittest.mock import patch

    from src.data import fetch

    good = _frame(_random_walk(300, seed=1))
    good.columns = pd.MultiIndex.from_product([["QQQ"], good.columns])

    calls = []

    def fake_download(tickers, **kwargs):
        calls.append(tickers)
        if isinstance(tickers, list):
            return good                      # SPY silently absent, like Yahoo does
        spy = _frame(_random_walk(300, seed=2))
        spy.columns = pd.MultiIndex.from_product([["SPY"], spy.columns])
        return spy

    with patch("src.data.yf.download", side_effect=fake_download):
        frames, problems = fetch(["SPY", "QQQ"], "daily", batch_size=25)

    assert len(calls) == 2, f"expected one batch call plus one retry, got {calls}"
    assert "SPY" in frames, "SPY should have been recovered via the retry path"
    assert not problems, f"expected no unresolved problems, got {problems}"
    print("  ok  a batch-omitted symbol is recovered via single-ticker retry")


def test_dotenv_never_shadows_a_real_env_var(tmp: Path) -> None:
    """A .env file must lose to an actual environment variable.

    On GitHub Actions the secrets arrive as real environment variables. If a
    committed or stale .env could override them, a scanner would silently post
    to the wrong webhook -- so the precedence is asserted rather than assumed.
    """
    import os

    from src.config import load_dotenv

    env_file = tmp / ".env"
    env_file.write_text(
        "# comment line\n"
        "\n"
        "BARBELL_FROM_FILE=file-value\n"
        "export BARBELL_EXPORTED=exported-value\n"
        "BARBELL_QUOTED=\"quoted value\"\n"
        "BARBELL_ALREADY_SET=file-should-lose\n"
        "MALFORMED_NO_EQUALS\n",
        encoding="utf-8",
    )

    keys = ["BARBELL_FROM_FILE", "BARBELL_EXPORTED", "BARBELL_QUOTED",
            "BARBELL_ALREADY_SET", "MALFORMED_NO_EQUALS"]
    for key in keys:
        os.environ.pop(key, None)
    os.environ["BARBELL_ALREADY_SET"] = "real-env-wins"

    try:
        load_dotenv(env_file)
        assert os.environ["BARBELL_FROM_FILE"] == "file-value"
        assert os.environ["BARBELL_EXPORTED"] == "exported-value", "export prefix"
        assert os.environ["BARBELL_QUOTED"] == "quoted value", "quotes stripped"
        assert os.environ["BARBELL_ALREADY_SET"] == "real-env-wins", (
            "a .env value overrode a real environment variable"
        )
        assert "MALFORMED_NO_EQUALS" not in os.environ
    finally:
        for key in keys:
            os.environ.pop(key, None)

    load_dotenv(tmp / "does-not-exist.env")   # must not raise
    print("  ok  .env parsed; real environment variables take precedence")


def test_symbol_normalisation() -> None:
    assert normalise(" aapl ") == "AAPL"
    assert normalise("$spy") == "SPY"
    assert normalise("^gspc") == "^GSPC"
    assert normalise("reliance.ns") == "RELIANCE.NS"
    assert normalise("btc-usd") == "BTC-USD"
    assert normalise("eurusd=x") == "EURUSD=X"
    assert normalise("brk-b") == "BRK-B"
    for bad in ("", "  ", "a" * 40, "drop table;", "AAPL MSFT", "../etc"):
        assert normalise(bad) == "", f"{bad!r} should be rejected"
    print("  ok  symbol normalisation and rejection")


def main() -> int:
    import tempfile

    print("\nbarbell scanner self-check\n" + "-" * 48)
    with tempfile.TemporaryDirectory() as raw:
        tmp = Path(raw)
        tests = [
            test_bollinger_sigma_is_actually_2_5,
            test_rsi_matches_wilder,
            test_indicator_columns_and_warmup,
            test_capitulation_fires,
            test_euphoria_fires,
            test_band_breach_without_rsi_stays_neutral,
            test_rsi_extreme_without_band_stays_neutral,
            test_forming_weekly_bar_is_dropped,
            test_forming_daily_bar_dropped_only_for_24_7_assets,
            test_streak_counting,
            test_insufficient_history_is_rejected,
            lambda: test_state_deduplicates(tmp),
            test_alert_rendering,
            test_retry_uses_ticker_first_column_order,
            test_batch_omission_recovers_via_retry,
            lambda: test_dotenv_never_shadows_a_real_env_var(tmp),
            test_symbol_normalisation,
        ]
        failures = 0
        for test in tests:
            try:
                test()
            except AssertionError as exc:
                failures += 1
                print(f"  FAIL  {getattr(test, '__name__', 'lambda')}: {exc}")
            except Exception as exc:
                failures += 1
                print(f"  ERROR {getattr(test, '__name__', 'lambda')}: "
                      f"{type(exc).__name__}: {exc}")

    print("-" * 48)
    print(f"{len(tests) - failures}/{len(tests)} passed\n")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
