"""Self-checks for the study layers (backtest + Monte Carlo).

    python tests/test_study.py

The two tests that matter most here are `test_ou_recovers_known_parameters`
and `test_ou_rejects_a_random_walk`. An OU fit will happily return numbers for
a series that has no mean reversion at all, and the simulation will then emit
confident-looking percentiles built on nothing. The Dickey-Fuller gate is the
only thing standing between the report and false precision, so it is verified
in both directions: it must recognise reversion when it is there, and refuse it
when it is not.
"""

from __future__ import annotations

import math
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import backtest as bt  # noqa: E402
from src import montecarlo as mc  # noqa: E402
from src.config import BBL_COL, BBM_COL, BBU_COL, RSI_COL  # noqa: E402
from src.signals import CAPITULATION  # noqa: E402


def _ou_series(b: float, sigma: float, n: int, seed: int = 0) -> np.ndarray:
    """Generate an AR(1) path with known parameters."""
    rng = np.random.default_rng(seed)
    noise = rng.standard_normal(n) * sigma
    z = np.zeros(n)
    for i in range(1, n):
        z[i] = b * z[i - 1] + noise[i]
    return z


def _signal_frame(trigger_positions: list[int], n: int = 400) -> pd.DataFrame:
    """A frame whose capitulation mask fires exactly at the given positions."""
    close = pd.Series(np.linspace(100.0, 200.0, n))
    df = pd.DataFrame({"Close": close})
    # Default: price above the lower band, momentum neutral -> no signal.
    df[BBL_COL] = close - 1.0
    df[BBU_COL] = close + 1.0
    df[BBM_COL] = close
    df[RSI_COL] = 50.0
    # Force both conditions true at the chosen bars.
    for pos in trigger_positions:
        df.loc[pos, BBL_COL] = close.iloc[pos] + 1.0
        df.loc[pos, RSI_COL] = 20.0
    df.index = pd.date_range("2020-01-01", periods=n, freq="B")
    return df


# ---------------------------------------------------------------------------
# Layer 1 -- event detection
# ---------------------------------------------------------------------------

def test_streaks_count_as_one_event() -> None:
    """A condition true for many consecutive bars is ONE observation."""
    mask = pd.Series([False] * 10 + [True] * 30 + [False] * 10)
    assert bt.event_positions(mask).tolist() == [10]
    print("  ok  a 30-bar streak counts as one event, not thirty")


def test_event_debounce_merges_retriggers() -> None:
    """An episode that blinks off and resumes must not become two events."""
    values = [False] * 10
    values += [True, False, True]           # re-trigger 2 bars apart
    values += [False] * 60
    values += [True]                        # genuinely separate episode
    mask = pd.Series(values)

    assert bt.event_positions(mask, min_gap=0).tolist() == [10, 12, 73]
    assert bt.event_positions(mask, min_gap=21).tolist() == [10, 73]
    print("  ok  re-triggers within the gap merge, distant episodes survive")


def test_forward_returns_and_mae_are_correct() -> None:
    """Verify the sliding-window maths against a hand-computed case."""
    close = np.array([100.0, 110.0, 90.0, 105.0, 120.0, 130.0])
    windows = bt._forward_matrix(close, 3)
    assert windows is not None
    # Row 0 covers close[0..3] = 100, 110, 90, 105
    entry = windows[0, 0]
    assert entry == 100.0
    assert math.isclose(windows[0, -1] / entry - 1, 0.05)          # 105/100
    path = windows[0, 1:]
    assert math.isclose(path.min() / entry - 1, -0.10)             # 90/100
    assert math.isclose(path.max() / entry - 1, 0.10)              # 110/100
    print("  ok  forward return, MAE and MFE match hand computation")


def test_incomplete_forward_windows_are_excluded() -> None:
    """An event too close to the end has no full horizon and must be dropped.

    Truncating the window and reporting the partial outcome as a full-horizon
    result is the classic way a bull market leaks into a backtest.
    """
    n = 400
    df = _signal_frame([50, 380], n=n)      # second event is 20 bars from the end
    result = bt.run(df, "TEST", "daily", CAPITULATION, horizons=(21, 63))

    assert result.n_events == 2, "both events should be detected"
    by_horizon = {s.horizon: s for s in result.stats}
    assert by_horizon[21].n_events == 1, "event at bar 380 has no complete 21d window"
    assert by_horizon[63].n_events == 1
    print("  ok  events without a complete forward window are excluded")


def test_backtest_reports_a_baseline() -> None:
    """Every conditional statistic must carry its unconditional comparison."""
    df = _signal_frame([50, 150, 250], n=400)
    result = bt.run(df, "TEST", "daily", CAPITULATION, horizons=(21,))
    stat = result.stats[0]

    assert stat.baseline_n > stat.n_events, "baseline must span the whole history"
    assert not math.isnan(stat.baseline_mean)
    # On a monotonically rising line every window is positive, so the signal
    # can add nothing -- edge must be ~0 rather than looking impressive.
    assert stat.win_rate == 1.0 and stat.baseline_win_rate == 1.0
    assert abs(stat.edge_mean) < 0.05, "a rising line offers no edge over holding"
    print("  ok  baseline reported; a trending series shows no spurious edge")


# ---------------------------------------------------------------------------
# Layer 2 -- OU fit and simulation
# ---------------------------------------------------------------------------

def test_ou_recovers_known_parameters() -> None:
    """Fit an AR(1) with known b and confirm the parameters come back."""
    b_true, sigma = 0.97, 0.01
    z = pd.Series(_ou_series(b_true, sigma, 4000, seed=1))
    fit = mc.fit_ou(z)

    assert abs(fit.b - b_true) < 0.01, f"b={fit.b:.4f}, expected ~{b_true}"
    assert abs(fit.sigma_eps - sigma) < 0.002
    expected_half_life = math.log(2) / -math.log(b_true)
    assert abs(fit.half_life - expected_half_life) < 4.0, (
        f"half-life {fit.half_life:.1f} vs expected {expected_half_life:.1f}"
    )
    assert fit.is_mean_reverting, "a genuine OU must pass the Dickey-Fuller gate"
    print(f"  ok  OU fit recovers b={fit.b:.3f}, half-life="
          f"{fit.half_life:.1f} (true {expected_half_life:.1f})")


def test_ou_rejects_a_random_walk() -> None:
    """A driftless random walk must NOT be reported as mean-reverting.

    This is the guard against the whole layer producing false confidence: a
    random-walking spread yields b ~ 1 and theta ~ 0, and without this gate the
    simulation still prints authoritative-looking percentiles.

    Asserted as a REJECTION RATE across many realisations, not on a single
    draw. A 5%-level test rejects a true null 5% of the time by construction,
    so a one-seed assertion is itself a coin flip that fails one run in twenty
    -- exactly what happened with seed 7 (b=0.9964, DF t=-4.11). Checking the
    size of the test is the correct way to validate a hypothesis test.
    """
    trials, rejects, slopes = 200, 0, []
    for seed in range(trials):
        rng = np.random.default_rng(seed)
        walk = pd.Series(np.cumsum(rng.standard_normal(1000) * 0.01))
        fit = mc.fit_ou(walk)
        slopes.append(fit.b)
        rejects += fit.is_mean_reverting

    rate = rejects / trials
    assert np.median(slopes) > 0.99, (
        f"random walk should give b~1, median {np.median(slopes):.4f}"
    )
    assert rate <= 0.12, (
        f"false-positive rate {rate:.1%} far exceeds the nominal 5% -- "
        "the Dickey-Fuller statistic is miscomputed"
    )
    print(f"  ok  random walk rejected {(1 - rate):.1%} of the time "
          f"(nominal 95%, median b={np.median(slopes):.4f})")


def test_ou_fit_needs_enough_observations() -> None:
    try:
        mc.fit_ou(pd.Series(np.zeros(20)))
        raise AssertionError("fit accepted 20 observations")
    except ValueError:
        pass
    print("  ok  OU fit refuses an undersized sample")


def _mc_frame(final_spread: float, n: int = 900, seed: int = 3) -> pd.DataFrame:
    """Frame with a flat baseline and an OU spread ending at `final_spread`.

    The long-run mean is centred on zero (the mid-band) and only the FINAL
    observation is set to the target spread. Shifting the whole series to land
    on the target instead -- the obvious construction -- moves the long-run
    mean by the same amount, so "starting below the baseline" would no longer
    mean "starting below the mean the process reverts to", and the test would
    measure nothing.
    """
    z = _ou_series(0.96, 0.02, n, seed=seed)
    z = z - z.mean()                        # long-run mean at the mid-band
    z[-1] = final_spread                    # current spread only; mu untouched
    baseline = 100.0
    df = pd.DataFrame({"Close": baseline * np.exp(z), BBM_COL: baseline})
    df.index = pd.date_range("2020-01-01", periods=n, freq="B")
    return df


def test_montecarlo_reversion_direction() -> None:
    """Reversion is toward the mid-band from whichever side we start on.

    Testing a fixed direction would report a euphoria as already reverted at
    step zero, since its spread is above the baseline from the outset.
    """
    below = mc.simulate(_mc_frame(-0.15), "LOW", "daily", horizon=60, n_paths=4000)
    above = mc.simulate(_mc_frame(+0.15), "HIGH", "daily", horizon=60, n_paths=4000)

    for result in (below, above):
        assert 0.0 < result.prob_reach_baseline < 1.0, (
            f"degenerate reversion probability {result.prob_reach_baseline}"
        )
        assert result.median_bars_to_baseline is None or (
            result.median_bars_to_baseline >= 1
        ), "reversion cannot occur at step zero"

    assert below.spread_now < 0 < above.spread_now
    # From below the mid-band, reverting means rising -> upside skew, and the
    # reverse from above.
    assert below.return_percentiles[50] > above.return_percentiles[50]
    print(f"  ok  reversion direction correct (below p50 "
          f"{below.return_percentiles[50]*100:+.1f}%, above p50 "
          f"{above.return_percentiles[50]*100:+.1f}%)")


def test_montecarlo_is_reproducible() -> None:
    frame = _mc_frame(-0.12)
    a = mc.simulate(frame, "T", "daily", horizon=40, n_paths=3000)
    b = mc.simulate(frame, "T", "daily", horizon=40, n_paths=3000)
    assert a.return_percentiles == b.return_percentiles
    assert a.prob_profit == b.prob_profit
    print("  ok  simulation is seeded and reproducible")


def test_montecarlo_percentiles_are_ordered() -> None:
    result = mc.simulate(_mc_frame(-0.10), "T", "daily", horizon=60, n_paths=6000)
    values = [result.return_percentiles[p] for p in sorted(result.return_percentiles)]
    assert values == sorted(values), f"percentiles out of order: {values}"
    assert result.expected_shortfall_5 <= result.return_percentiles[5], (
        "expected shortfall must sit at or below the 5th percentile"
    )
    assert 0.0 <= result.prob_profit <= 1.0
    assert result.prob_drawdown_20 <= result.prob_drawdown_10, (
        "a -20% path is a subset of the -10% paths"
    )
    print("  ok  percentiles ordered, shortfall and drawdown probabilities coherent")


def main() -> int:
    print("\nstudy layer self-check\n" + "-" * 52)
    tests = [
        test_streaks_count_as_one_event,
        test_event_debounce_merges_retriggers,
        test_forward_returns_and_mae_are_correct,
        test_incomplete_forward_windows_are_excluded,
        test_backtest_reports_a_baseline,
        test_ou_recovers_known_parameters,
        test_ou_rejects_a_random_walk,
        test_ou_fit_needs_enough_observations,
        test_montecarlo_reversion_direction,
        test_montecarlo_is_reproducible,
        test_montecarlo_percentiles_are_ordered,
    ]
    failures = 0
    for test in tests:
        try:
            test()
        except AssertionError as exc:
            failures += 1
            print(f"  FAIL  {test.__name__}: {exc}")
        except Exception as exc:
            failures += 1
            print(f"  ERROR {test.__name__}: {type(exc).__name__}: {exc}")

    print("-" * 52)
    print(f"{len(tests) - failures}/{len(tests)} passed\n")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
