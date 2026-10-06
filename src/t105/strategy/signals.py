"""Alpha sleeves. Every function is causal: row t uses only bars closed at or before t.

All signals are *volatility-normalised* (a 2% move in a 1%-vol coin and a 6%
move in a 3%-vol coin are the same statistical event) and squashed with tanh,
so no single outlier print (very common in crypto) can dominate a position.

Sleeves (each returns a time x asset frame of scores in roughly [-1, 1]):
  trend      -- multi-horizon time-series momentum, |score| ~ strength of trend
  xsmom      -- cross-sectional momentum on *residual* (beta-adjusted) returns,
                skipping the most recent hours (where short-term reversal lives)
  reversal   -- short-horizon residual mean reversion (cross-sectional)
"""

from __future__ import annotations

import numpy as np
import pandas as pd


def log_returns(close: pd.DataFrame) -> pd.DataFrame:
    return np.log(close).diff()


def ewm_vol(r: pd.DataFrame, halflife: float = 72, min_periods: int = 48) -> pd.DataFrame:
    """EWMA volatility of hourly returns (RiskMetrics-style, zero mean)."""
    return np.sqrt((r**2).ewm(halflife=halflife, min_periods=min_periods).mean())


def market_return(r: pd.DataFrame, eligible: pd.DataFrame) -> pd.Series:
    """Equal-weight return of eligible assets: the 'crypto beta' factor."""
    return r.where(eligible).mean(axis=1)


def rolling_beta(r: pd.DataFrame, rm: pd.Series, window: int = 336) -> pd.DataFrame:
    cov = r.mul(rm, axis=0).rolling(window, min_periods=window // 2).mean() - r.rolling(
        window, min_periods=window // 2
    ).mean().mul(rm.rolling(window, min_periods=window // 2).mean(), axis=0)
    var = rm.rolling(window, min_periods=window // 2).var(ddof=0)
    beta = cov.div(var, axis=0)
    # shrink toward 1 (Vasicek-style): betas estimated from 2 weeks of hourly data are noisy
    return (0.6 * beta + 0.4).clip(0.2, 3.0)


def xs_rank(x: pd.DataFrame, eligible: pd.DataFrame) -> pd.DataFrame:
    """Cross-sectional rank mapped to [-1, 1] among eligible assets."""
    x = x.where(eligible)
    rk = x.rank(axis=1)
    n = x.notna().sum(axis=1)
    out = rk.sub(1).div((n - 1).clip(lower=1), axis=0) * 2 - 1
    return out[n >= 5].reindex(out.index)


def trend_sleeve(
    logp: pd.DataFrame, vol: pd.DataFrame, horizons=(24, 72, 168, 336), squash: float = 1.5
) -> pd.DataFrame:
    parts = []
    for h in horizons:
        z = (logp - logp.shift(h)) / (vol * np.sqrt(h))
        parts.append(np.tanh(z / squash))
    return sum(parts) / len(parts)


def xsmom_sleeve(
    resid: pd.DataFrame, eligible: pd.DataFrame, vol: pd.DataFrame, lookback: int = 168, skip: int = 6
) -> pd.DataFrame:
    cum = resid.rolling(lookback - skip, min_periods=(lookback - skip) // 2).sum().shift(skip)
    z = cum / (vol * np.sqrt(lookback - skip))
    return xs_rank(z, eligible)


def reversal_sleeve(resid: pd.DataFrame, eligible: pd.DataFrame, vol: pd.DataFrame, lookback: int = 6) -> pd.DataFrame:
    cum = resid.rolling(lookback, min_periods=lookback).sum()
    z = cum / (vol * np.sqrt(lookback))
    return -xs_rank(z, eligible)


def trend_gate_sleeve(logp: pd.DataFrame, lookbacks=(168, 336, 720)) -> pd.DataFrame:
    """Ensemble of slow trend gates: mean over lookbacks of sign(log return over lb).

    Values in {-1, -1/3, +1/3, +1}. Averaging gates over several lookbacks instead
    of tuning one is the robustness choice: research showed the Sharpe surface is
    smooth across +/-25% lookback perturbations, whereas single lookbacks vary a lot.
    """
    return sum(np.sign(logp - logp.shift(lb)) for lb in lookbacks) / len(lookbacks)


def trend_gate_hysteresis(
    logp: pd.DataFrame, vol: pd.DataFrame, lookbacks=(168, 336, 720), dead_band: float = 0.25
) -> pd.DataFrame:
    """Same gates, but each one only switches when the vol-normalised trend
    z = log(P_t/P_{t-lb}) / (sigma_1h * sqrt(lb)) leaves a dead band of +/-dead_band;
    inside the band the previous state is carried (causal ffill).

    Why: evaluated hourly, a gate whose trend sits near zero flips back and forth
    every few hours, and each flip turns over a whole position (measured: 110x NAV
    turnover vs ~10x for the same rule sampled daily). The band removes noise
    crossings without delaying genuine trend changes by more than a few hours.
    """
    parts = []
    for lb in lookbacks:
        z = (logp - logp.shift(lb)) / (vol * np.sqrt(lb))
        g = pd.DataFrame(
            np.where(z > dead_band, 1.0, np.where(z < -dead_band, -1.0, np.nan)), index=z.index, columns=z.columns
        )
        g = g.ffill().where(z.notna()).fillna(0.0)
        parts.append(g)
    return sum(parts) / len(parts)


def eligibility(
    close: pd.DataFrame, quote_vol: pd.DataFrame, min_history: int = 24 * 30, min_daily_usd: float = 5e6
) -> pd.DataFrame:
    """Asset is tradeable at t if it has enough history and real liquidity."""
    hist_ok = close.notna().astype(int).rolling(min_history, min_periods=1).sum() >= min_history
    adv = quote_vol.rolling(24 * 14, min_periods=24 * 7).sum() / 14
    return hist_ok & (adv >= min_daily_usd) & close.notna()
