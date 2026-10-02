"""Market-state (regime) features computed from the whole universe.

Using the cross-section as information is something single-asset bots cannot
do: breadth, dispersion and average correlation describe *what kind of market
we are in*, which decides which alpha sleeve should be trusted and how much
gross risk to run.

Outputs (time-indexed):
  mkt_trend    in [-1,1]  vol-normalised trend of the equal-weight index
  breadth      in [0,1]   share of assets above their 7-day EMA
  vol_ratio    >0         short-term index vol / its 30-day median (>1 = stressed)
  avg_corr     in [-1,1]  mean pairwise correlation over 3 days (high = no diversification)
  dispersion   >=0        cross-sectional std of 24h residual returns
  risk_on      in [0,1]   composite risk appetite used to scale net exposure
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def _avg_pairwise_corr(r: pd.DataFrame, window: int) -> pd.Series:
    """Average pairwise correlation via the variance identity:
    var(sum z) = N + N(N-1) * rho_bar  for standardised z. O(T*N) instead of O(T*N^2)."""
    mu = r.rolling(window, min_periods=window // 2).mean()
    sd = r.rolling(window, min_periods=window // 2).std()
    z = (r - mu) / sd
    n = z.notna().sum(axis=1)
    s = z.sum(axis=1, min_count=1)
    var_s = (s ** 2).rolling(window, min_periods=window // 2).mean()
    nbar = n.rolling(window, min_periods=1).mean()
    rho = (var_s - nbar) / (nbar * (nbar - 1)).clip(lower=1)
    return rho.clip(-1, 1)


def regime_features(logp: pd.DataFrame, r: pd.DataFrame, rm: pd.Series, resid: pd.DataFrame,
                    eligible: pd.DataFrame) -> pd.DataFrame:
    idx = rm.fillna(0).cumsum()
    vol_m = np.sqrt((rm ** 2).ewm(halflife=72, min_periods=48).mean())
    trend = 0.0
    for h in (72, 168, 336):
        trend = trend + np.tanh(((idx - idx.shift(h)) / (vol_m * np.sqrt(h))) / 1.5)
    trend = trend / 3

    ema = logp.ewm(span=168, min_periods=96).mean()
    above = (logp > ema).where(eligible)
    breadth = above.mean(axis=1)

    vol_short = np.sqrt((rm ** 2).ewm(halflife=12, min_periods=12).mean())
    vol_ratio = vol_short / vol_short.rolling(24 * 30, min_periods=24 * 7).median()

    avg_corr = _avg_pairwise_corr(r.where(eligible), 72)
    dispersion = resid.rolling(24, min_periods=12).sum().where(eligible).std(axis=1)

    # Risk appetite: trend and breadth agree -> risk on; stressed vol -> scale down.
    raw = 0.5 * (trend + 1) * 0.6 + breadth.fillna(0.5) * 0.4
    stress = (1.0 / vol_ratio.clip(lower=1.0)).fillna(1.0) ** 0.5
    risk_on = (raw * stress).clip(0, 1)
    return pd.DataFrame({"mkt_trend": trend, "breadth": breadth, "vol_ratio": vol_ratio,
                         "avg_corr": avg_corr, "dispersion": dispersion, "risk_on": risk_on})
