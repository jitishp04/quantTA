"""Layer 2 -- Ornstein-Uhlenbeck Monte Carlo on the deviation from the mean.

Why not plain GBM
-----------------
The obvious Monte Carlo -- fit drift and volatility to log returns, simulate
geometric Brownian motion forward -- cannot answer "is this a good buy",
because GBM is memoryless. It random-walks from wherever the price happens to
sit and has no idea the price is currently 2.5 sigma below its own 200-period
mean. Feed it a capitulation and it reports "volatile, could go either way",
which is exactly as useful as not running it.

What this models instead
------------------------
The quantity being simulated is the SPREAD, not the price:

    z = log(Close) - log(BBM_200)

i.e. log distance from the 200-period baseline -- the same baseline the
Bollinger bands are built on. A capitulation is by construction a large
negative z. Modelling z as Ornstein-Uhlenbeck,

    dz = theta * (mu - z) dt + sigma dW

gives the mean reversion the signal is implicitly betting on, and yields
directly interpretable quantities: a reversion half-life in candles, the
probability of getting back to the mid-band inside a horizon, and how much
further underwater the path goes first.

Discretely this is an AR(1), z_t = a + b*z_{t-1} + eps, fitted by OLS, with
theta = -ln(b) and half-life = ln(2)/theta.

The honesty check
-----------------
Fit an OU to a trending series and you get b ~ 1, theta ~ 0, and a model that
has quietly degenerated into a random walk -- while still happily emitting
confident-looking percentiles. So the fit is tested with the Dickey-Fuller
statistic (the t-ratio on b-1, which is precisely the DF test) and the result
carries `is_mean_reverting`. When that is False the simulation output is not
evidence of anything and the report says so rather than printing numbers that
look authoritative.

Baseline treatment: BBM_200 is held FLAT over the simulation horizon. Letting
it drift upward at its recent slope would bake an assumed bull market into
every path and inflate the reversion probability. Holding it flat isolates the
mean-reversion component, which is the thing being measured, and is the
conservative choice for a buy signal.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from .config import BBM_COL

# 5% Dickey-Fuller critical value, constant without trend. More negative than
# this rejects the unit root, i.e. the spread is mean-reverting.
DF_CRITICAL_5PCT = -2.86

DEFAULT_PATHS = 20_000
SIM_SEED = 20260903
PERCENTILES = (5, 25, 50, 75, 95)


@dataclass
class OUFit:
    """AR(1) / Ornstein-Uhlenbeck parameters for the log spread."""

    a: float                 # AR(1) intercept
    b: float                 # AR(1) slope; b = exp(-theta)
    sigma_eps: float         # residual standard deviation, per candle
    theta: float             # reversion speed, per candle
    mu: float                # long-run mean of the spread
    half_life: float         # candles to close half the gap
    df_stat: float           # Dickey-Fuller t-statistic on (b - 1)
    r_squared: float
    n_obs: int

    @property
    def is_mean_reverting(self) -> bool:
        return 0.0 < self.b < 1.0 and self.df_stat < DF_CRITICAL_5PCT


@dataclass
class MonteCarloResult:
    ticker: str
    timeframe: str
    horizon: int
    n_paths: int
    spot: float
    baseline: float           # BBM_200 at the simulation start
    spread_now: float         # z0, log distance from baseline
    fit: OUFit
    return_percentiles: dict[int, float] = field(default_factory=dict)
    prob_profit: float = float("nan")
    prob_reach_baseline: float = float("nan")
    median_bars_to_baseline: float | None = None
    prob_drawdown_10: float = float("nan")
    prob_drawdown_20: float = float("nan")
    expected_shortfall_5: float = float("nan")   # mean of the worst 5% of outcomes

    @property
    def reversion_actionable(self) -> bool:
        """Is reversion fast enough to matter inside this horizon?

        Statistical significance and practical usefulness are different tests.
        A spread can clear the Dickey-Fuller gate with b = 0.9964 -- formally
        mean-reverting, but a half-life of 192 candles, which over a 63-candle
        horizon closes barely a fifth of the gap. Passing the hypothesis test
        is not the same as the trade working in the time you are giving it.
        """
        return self.fit.half_life <= self.horizon

    @property
    def trustworthy(self) -> bool:
        return self.fit.is_mean_reverting and self.reversion_actionable


def spread(df: pd.DataFrame) -> pd.Series:
    """Log distance from the 200-period Bollinger baseline."""
    return np.log(df["Close"]) - np.log(df[BBM_COL])


def fit_ou(z: pd.Series) -> OUFit:
    """Fit an AR(1) to the spread by OLS and derive the OU parameters."""
    values = z.dropna().to_numpy(dtype="float64")
    if values.size < 60:
        raise ValueError(f"need >=60 spread observations to fit, got {values.size}")

    lagged, current = values[:-1], values[1:]
    design = np.column_stack([np.ones_like(lagged), lagged])
    coefficients, *_ = np.linalg.lstsq(design, current, rcond=None)
    a, b = float(coefficients[0]), float(coefficients[1])

    residuals = current - design @ coefficients
    dof = lagged.size - 2
    resid_var = float(residuals @ residuals) / dof
    covariance = np.linalg.inv(design.T @ design)
    se_b = math.sqrt(resid_var * covariance[1, 1])

    total_var = float(((current - current.mean()) ** 2).sum())
    r_squared = 1.0 - float(residuals @ residuals) / total_var if total_var else float("nan")

    # theta and half-life are only meaningful for a stationary fit.
    if 0.0 < b < 1.0:
        theta = -math.log(b)
        half_life = math.log(2.0) / theta
    else:
        theta = 0.0
        half_life = float("inf")

    return OUFit(
        a=a,
        b=b,
        sigma_eps=math.sqrt(resid_var),
        theta=theta,
        mu=a / (1.0 - b) if b != 1.0 else float("nan"),
        half_life=half_life,
        df_stat=(b - 1.0) / se_b if se_b else float("nan"),
        r_squared=r_squared,
        n_obs=int(lagged.size),
    )


def simulate(
    df: pd.DataFrame,
    ticker: str,
    timeframe: str,
    horizon: int,
    n_paths: int = DEFAULT_PATHS,
    fit_window: int | None = 756,
    seed: int = SIM_SEED,
) -> MonteCarloResult:
    """Project the spread forward and translate the paths back into prices.

    The time loop below steps the recursion candle by candle, but every step
    advances all `n_paths` simultaneously as one numpy operation -- the loop is
    over the horizon (tens to hundreds), never over paths (tens of thousands).
    A recursive process cannot be vectorised along its own time axis.
    """
    z = spread(df).dropna()
    if fit_window:
        z = z.tail(fit_window)
    fit = fit_ou(z)

    z0 = float(z.iloc[-1])
    baseline = float(df[BBM_COL].iloc[-1])
    spot = float(df["Close"].iloc[-1])

    rng = np.random.default_rng(seed)
    state = np.full(n_paths, z0, dtype="float64")
    paths = np.empty((horizon, n_paths), dtype="float64")
    for step in range(horizon):
        state = fit.a + fit.b * state + fit.sigma_eps * rng.standard_normal(n_paths)
        paths[step] = state

    # z = log(P) - log(BBM), and BBM is held flat, so P = baseline * exp(z).
    prices = baseline * np.exp(paths)
    returns = prices / spot - 1.0

    terminal = returns[-1]
    path_min = returns.min(axis=0)

    # First passage back to the baseline. The mid-band is z = 0 by definition,
    # but "reverting" means approaching it from whichever side we start on: a
    # capitulation (z0 < 0) reverts upward, a euphoria (z0 > 0) reverts
    # downward. Testing a fixed direction would report a euphoria as already
    # reverted at step zero.
    reached = paths >= 0.0 if z0 < 0 else paths <= 0.0
    ever = reached.any(axis=0)
    if ever.any():
        first = np.argmax(reached[:, ever], axis=0) + 1
        median_bars = float(np.median(first))
    else:
        median_bars = None

    worst_5 = terminal[terminal <= np.percentile(terminal, 5)]

    return MonteCarloResult(
        ticker=ticker,
        timeframe=timeframe,
        horizon=horizon,
        n_paths=n_paths,
        spot=spot,
        baseline=baseline,
        spread_now=z0,
        fit=fit,
        return_percentiles={p: float(np.percentile(terminal, p)) for p in PERCENTILES},
        prob_profit=float((terminal > 0).mean()),
        prob_reach_baseline=float(ever.mean()),
        median_bars_to_baseline=median_bars,
        prob_drawdown_10=float((path_min <= -0.10).mean()),
        prob_drawdown_20=float((path_min <= -0.20).mean()),
        expected_shortfall_5=float(worst_5.mean()) if worst_5.size else float("nan"),
    )
