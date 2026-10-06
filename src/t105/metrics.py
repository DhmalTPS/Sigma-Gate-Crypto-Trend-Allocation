"""Performance + statistical-confidence metrics.

The organizer scores 0.4*Sortino + 0.3*Sharpe + 0.3*Calmar but has not published
the annualisation convention. We compute everything from the NAV path with an
explicit `periods_per_year` (crypto trades 24/7: hourly -> 8760) so that
strategies are always compared on one convention. Note the asymmetry this
creates: over a 14-day window an annualised Calmar = (annualised return / MDD)
can be an order of magnitude larger than Sharpe/Sortino, so *max drawdown is the
single most leveraged quantity in the composite* -- the risk layer is designed
around that.

Because 14 days of crypto returns are extremely noisy, point estimates are not
enough. We also report:
  * Probabilistic Sharpe Ratio (Bailey & Lopez de Prado 2012): P(true SR > SR*)
    correcting for skew and fat tails of the return distribution.
  * Deflated Sharpe threshold for N trials (multiple-testing correction).
  * Stationary block-bootstrap confidence intervals (Politis & Romano 1994),
    which respect volatility clustering / autocorrelation.
"""

from __future__ import annotations

import math

import numpy as np
import pandas as pd

HOURS_PER_YEAR = 24 * 365


def returns_from_nav(nav: pd.Series) -> pd.Series:
    return nav.pct_change().dropna()


def max_drawdown(nav: pd.Series | np.ndarray) -> float:
    x = np.asarray(nav, dtype=float)
    if len(x) == 0:
        return 0.0
    peak = np.maximum.accumulate(x)
    return float(np.max(1 - x / peak))


def sharpe(r: np.ndarray, ppy: float = HOURS_PER_YEAR) -> float:
    r = np.asarray(r, float)
    sd = r.std(ddof=1) if len(r) > 1 else 0.0
    return float(r.mean() / sd * math.sqrt(ppy)) if sd > 0 else 0.0


def sortino(r: np.ndarray, ppy: float = HOURS_PER_YEAR) -> float:
    r = np.asarray(r, float)
    dd = np.sqrt(np.mean(np.minimum(r, 0.0) ** 2)) if len(r) else 0.0  # target = 0
    return float(r.mean() / dd * math.sqrt(ppy)) if dd > 0 else 0.0


def calmar(nav: np.ndarray, ppy: float = HOURS_PER_YEAR, annualise: bool = True) -> float:
    nav = np.asarray(nav, float)
    if len(nav) < 2:
        return 0.0
    total = nav[-1] / nav[0] - 1
    ret = (1 + total) ** (ppy / (len(nav) - 1)) - 1 if annualise else total
    mdd = max_drawdown(nav)
    return float(ret / mdd) if mdd > 1e-12 else 0.0


def composite(nav: np.ndarray, ppy: float = HOURS_PER_YEAR, annualise_calmar: bool = True) -> float:
    r = np.diff(nav) / nav[:-1]
    return 0.4 * sortino(r, ppy) + 0.3 * sharpe(r, ppy) + 0.3 * calmar(nav, ppy, annualise_calmar)


def probabilistic_sharpe(r: np.ndarray, sr_benchmark: float = 0.0) -> float:
    """PSR on per-period (non-annualised) Sharpe. Returns P(SR_true > benchmark)."""
    r = np.asarray(r, float)
    n = len(r)
    if n < 10 or r.std(ddof=1) == 0:
        return 0.5
    sr = r.mean() / r.std(ddof=1)
    z = (r - r.mean()) / r.std(ddof=0)
    skew, kurt = float(np.mean(z**3)), float(np.mean(z**4))
    denom = math.sqrt(max(1e-12, 1 - skew * sr + (kurt - 1) / 4 * sr**2))
    stat = (sr - sr_benchmark) * math.sqrt(n - 1) / denom
    return float(0.5 * (1 + math.erf(stat / math.sqrt(2))))


def deflated_sharpe_threshold(n_trials: int, sr_std_across_trials: float) -> float:
    """Expected max per-period SR among n_trials zero-skill strategies."""
    if n_trials < 2:
        return 0.0
    g = 0.5772156649
    from statistics import NormalDist

    nd = NormalDist()
    return sr_std_across_trials * ((1 - g) * nd.inv_cdf(1 - 1 / n_trials) + g * nd.inv_cdf(1 - 1 / (n_trials * math.e)))


def stationary_bootstrap(r: np.ndarray, stat, n_boot: int = 1000, mean_block: int = 24, seed: int = 7) -> np.ndarray:
    """Politis-Romano stationary bootstrap of `stat(resampled_returns)`."""
    rng = np.random.default_rng(seed)
    r = np.asarray(r, float)
    n = len(r)
    p = 1.0 / mean_block
    out = np.empty(n_boot)
    for b in range(n_boot):
        idx = np.empty(n, dtype=int)
        idx[0] = rng.integers(n)
        jumps = rng.random(n) < p
        starts = rng.integers(n, size=n)
        for t in range(1, n):
            idx[t] = starts[t] if jumps[t] else (idx[t - 1] + 1) % n
        out[b] = stat(r[idx])
    return out


def summary(
    nav: pd.Series, ppy: float = HOURS_PER_YEAR, fees_paid: float = 0.0, turnover: float = 0.0, n_trades: int = 0
) -> dict:
    nav = nav.dropna()
    r = returns_from_nav(nav).values
    total = float(nav.iloc[-1] / nav.iloc[0] - 1) if len(nav) > 1 else 0.0
    return {
        "total_return": total,
        "ann_vol": float(np.std(r, ddof=1) * math.sqrt(ppy)) if len(r) > 1 else 0.0,
        "sharpe": sharpe(r, ppy),
        "sortino": sortino(r, ppy),
        "max_drawdown": max_drawdown(nav.values),
        "calmar": calmar(nav.values, ppy),
        "calmar_raw": calmar(nav.values, ppy, annualise=False),
        "composite": composite(nav.values, ppy),
        "psr_vs_0": probabilistic_sharpe(r),
        "fees_paid": fees_paid,
        "turnover_x_nav": turnover,
        "n_trades": n_trades,
        "periods": len(r),
    }


def rolling_window_stats(nav: pd.Series, window: int, step: int, ppy: float = HOURS_PER_YEAR) -> pd.DataFrame:
    """Distribution of competition-length outcomes: slide a window (e.g. 14 days)
    over the backtest and score each one as if it were the live competition."""
    rows = []
    v = nav.values
    for s in range(0, len(v) - window, step):
        seg = v[s : s + window + 1]
        r = np.diff(seg) / seg[:-1]
        rows.append(
            {
                "start": nav.index[s],
                "ret": seg[-1] / seg[0] - 1,
                "mdd": max_drawdown(seg),
                "sharpe": sharpe(r, ppy),
                "sortino": sortino(r, ppy),
                "calmar_raw": calmar(seg, ppy, False),
                "composite": composite(seg, ppy),
            }
        )
    return pd.DataFrame(rows)
