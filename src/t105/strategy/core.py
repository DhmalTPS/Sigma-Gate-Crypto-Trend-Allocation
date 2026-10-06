"""Strategy core: data panels -> sleeves -> adaptive ensemble -> raw target weights.

This vectorised stage is identical in backtest and live (live simply runs it on
the most recent ~60 days and takes the last row). Everything path-dependent
(drawdown, current holdings, hysteresis, no-trade bands) lives in
portfolio/policy.py, which is also shared by both modes.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from . import signals as S
from .regime import regime_features


@dataclass
class StrategyParams:
    vol_halflife: float = 72
    trend_horizons: tuple = (24, 72, 168, 336)
    xsmom_lookback: int = 168
    xsmom_skip: int = 6
    reversal_lookback: int = 6
    min_daily_usd: float = 5e6
    min_history_h: int = 24 * 30
    max_tick_bps: float = 5.0  # exclude coins whose 1-tick spread is expensive
    universe: tuple = ()  # whitelist; empty = all eligible crypto pairs
    gate_lookbacks: tuple = (168, 336, 720)
    gate_dead_band: float = 0.5  # 0 = plain sign gates (research R6: plateau 0.25-1.0)
    # ensemble
    sleeves: tuple = ("trend", "xsmom", "reversal")
    prior_weights: dict = field(default_factory=lambda: {"trend": 0.45, "xsmom": 0.35, "reversal": 0.20})
    perf_halflife_h: float = 24 * 10  # memory of sleeve performance
    shrink: float = 0.6  # 1 = always prior, 0 = pure performance chasing
    softmax_temp: float = 1.0
    sleeve_cost_bps: float = 8.0  # cost charged to sleeve paper-portfolios per unit turnover


@dataclass
class StrategyOutput:
    alpha: pd.DataFrame  # combined score per asset, ~[-1, 1]
    vol: pd.DataFrame  # hourly EWMA vol per asset
    eligible: pd.DataFrame
    regime: pd.DataFrame
    sleeve_weights: pd.DataFrame  # time x sleeve
    sleeves: dict
    returns: pd.DataFrame  # simple hourly returns (for covariance)


def tick_bps(close: pd.DataFrame, price_precision: dict[str, int]) -> pd.DataFrame:
    tick = pd.Series({c: 10.0 ** -price_precision.get(c, 8) for c in close.columns})
    return (1e4 * tick / close).reindex(columns=close.columns)


def _sleeve_paper_returns(score: pd.DataFrame, r_next: pd.DataFrame, vol: pd.DataFrame, cost_bps: float) -> pd.Series:
    """Dollar-neutral-ish paper portfolio for each sleeve: w ~ score/vol, unit gross.
    Return realised at t uses weights from t-1 (no look-ahead)."""
    w = (score / (vol * np.sqrt(24))).replace([np.inf, -np.inf], np.nan).fillna(0)
    w = w.div(w.abs().sum(axis=1).replace(0, np.nan), axis=0).fillna(0)
    gross = (w.shift(1) * r_next).sum(axis=1)
    turnover = (w - w.shift(1)).abs().sum(axis=1)
    return gross - turnover * cost_bps / 1e4


def adaptive_sleeve_weights(paper: pd.DataFrame, p: StrategyParams) -> pd.DataFrame:
    """Shrunk softmax of each sleeve's exponentially-weighted information ratio.

    w_k = shrink * prior_k + (1-shrink) * softmax(IR_k / temp)
    A sleeve that has been losing money is down-weighted gradually, not
    switched off after a couple of bad hours -- with 14 days of live data,
    hard on/off switching would mostly be fitting noise.
    """
    mu = paper.ewm(halflife=p.perf_halflife_h, min_periods=48).mean()
    sd = paper.ewm(halflife=p.perf_halflife_h, min_periods=48).std()
    ir = (mu / sd * np.sqrt(24)).fillna(0).clip(-3, 3)  # daily-scaled IR, bounded
    ex = np.exp(ir / p.softmax_temp)
    soft = ex.div(ex.sum(axis=1), axis=0)
    prior = pd.Series(p.prior_weights).reindex(paper.columns).fillna(0)
    prior = prior / prior.sum()
    w = soft.mul(1 - p.shrink).add(prior * p.shrink, axis=1)
    return w.shift(1).fillna(prior)  # use information up to t-1 only


def compute(
    close: pd.DataFrame, quote_vol: pd.DataFrame, p: StrategyParams, price_precision: dict[str, int] | None = None
) -> StrategyOutput:
    close = close.sort_index()
    logp = np.log(close)
    lr = logp.diff()
    r = close.pct_change(fill_method=None)
    vol = S.ewm_vol(lr, p.vol_halflife)
    elig = S.eligibility(close, quote_vol, p.min_history_h, p.min_daily_usd)
    if price_precision:
        elig &= tick_bps(close, price_precision) <= p.max_tick_bps
    if p.universe:
        elig &= pd.DataFrame(
            np.isin(close.columns, p.universe)[None, :].repeat(len(close), 0), index=close.index, columns=close.columns
        )
    rm = S.market_return(lr, elig)
    beta = S.rolling_beta(lr, rm)
    resid = lr - beta.mul(rm, axis=0)

    sleeves = {}
    if "trend" in p.sleeves:
        sleeves["trend"] = S.trend_sleeve(logp, vol, p.trend_horizons).where(elig)
    if "xsmom" in p.sleeves:
        sleeves["xsmom"] = S.xsmom_sleeve(resid, elig, vol, p.xsmom_lookback, p.xsmom_skip)
    if "tgate" in p.sleeves:
        if p.gate_dead_band > 0:
            sleeves["tgate"] = S.trend_gate_hysteresis(logp, vol, p.gate_lookbacks, p.gate_dead_band).where(elig)
        else:
            sleeves["tgate"] = S.trend_gate_sleeve(logp, p.gate_lookbacks).where(elig)
    if "reversal" in p.sleeves:
        sleeves["reversal"] = S.reversal_sleeve(resid, elig, vol, p.reversal_lookback)

    paper = pd.DataFrame(
        {k: _sleeve_paper_returns(v.fillna(0), r.fillna(0), vol, p.sleeve_cost_bps) for k, v in sleeves.items()}
    )
    sw = adaptive_sleeve_weights(paper, p)
    alpha = sum(sleeves[k].fillna(0).mul(sw[k], axis=0) for k in sleeves)
    alpha = alpha.where(elig)
    reg = regime_features(logp, lr, rm, resid, elig)
    return StrategyOutput(alpha, vol, elig, reg, sw, sleeves, r)
