"""Path-dependent portfolio policy shared by backtest and live.

alpha row (+ regime, vol, covariance, current weights, NAV path)
  -> entry/exit hysteresis       (no churn around a threshold)
  -> risk-budgeted raw weights   (w ~ alpha / vol)
  -> regime net-exposure scaling (risk_on decides how much crypto beta we carry)
  -> covariance-aware vol target (portfolio risk, not sum of single-asset risks)
  -> hard caps                   (per asset, gross <= 1x NAV: spot, no leverage)
  -> drawdown governor           (Calmar = return / MaxDD: protect the path)
  -> no-trade band               (only trade when the change is worth the fee)
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd


@dataclass
class PolicyParams:
    mode: str = "long_only"      # long_only | long_short | neutral
    use_regime_gross: bool = False    # research: trend-timing of gross exposure did NOT help
    enter: float = 0.30          # |alpha| to open a position
    exit: float = 0.10           # |alpha| below which an open position is closed
    allow_short: bool = True
    short_regime_max: float = -0.25   # shorts only when market trend below this
    short_scale: float = 0.5     # shorts are sized smaller (fee 0.1% both legs + squeeze risk)
    target_vol_ann: float = 0.30
    max_gross: float = 0.95      # <= 1.0 by rule; keep 5% cash buffer for fees/rounding
    max_weight: float = 0.20
    max_weight_major: float = 0.35
    majors: tuple = ("BTC/USD", "ETH/USD")
    min_risk_on_gross: float = 0.15   # floor on gross budget even in risk-off
    cov_window: int = 24 * 14
    cov_shrink: float = 0.3      # shrink correlation matrix toward constant-correlation
    dd_soft: float = 0.03        # start de-risking at 3% drawdown from peak
    dd_hard: float = 0.10        # minimum exposure reached at 10%
    dd_floor: float = 0.25
    band_abs: float = 0.02       # do not trade a name unless |dw| > 2% NAV ...
    band_rel: float = 0.25       # ... or > 25% of its target
    min_trade_usd: float = 50.0


@dataclass
class PolicyState:
    peak_nav: float = 0.0
    active: dict = field(default_factory=dict)   # pair -> +1 / -1 for open positions (hysteresis)


def shrunk_cov(returns: np.ndarray, shrink: float) -> np.ndarray:
    """Sample covariance with correlation shrunk toward its average (constant-corr target)."""
    x = returns - np.nanmean(returns, axis=0)
    x = np.nan_to_num(x)
    cov = x.T @ x / max(1, len(x) - 1)
    sd = np.sqrt(np.clip(np.diag(cov), 1e-12, None))
    corr = cov / np.outer(sd, sd)
    n = len(sd)
    rho = (corr.sum() - n) / max(1, n * (n - 1))
    target = np.full_like(corr, rho)
    np.fill_diagonal(target, 1.0)
    corr_s = (1 - shrink) * corr + shrink * target
    return corr_s * np.outer(sd, sd)


def drawdown_multiplier(nav: float, peak: float, p: PolicyParams) -> float:
    dd = 1 - nav / peak if peak > 0 else 0.0
    if dd <= p.dd_soft:
        return 1.0
    if dd >= p.dd_hard:
        return p.dd_floor
    frac = (dd - p.dd_soft) / (p.dd_hard - p.dd_soft)
    return 1.0 - frac * (1.0 - p.dd_floor)


def target_weights(alpha: pd.Series, vol: pd.Series, regime: pd.Series, recent_returns: pd.DataFrame,
                   current_w: pd.Series, nav: float, st: PolicyState, p: PolicyParams,
                   shorts_enabled: bool = True) -> tuple[pd.Series, dict]:
    """Return (final target weights, diagnostics). Weights are signed fractions of NAV."""
    st.peak_nav = max(st.peak_nav, nav)
    a = alpha.dropna()
    trend = float(regime.get("mkt_trend", 0.0) or 0.0)
    risk_on = float(np.clip(regime.get("risk_on", 0.5) if pd.notna(regime.get("risk_on")) else 0.5, 0, 1))
    if p.mode == "long_only" or not (p.allow_short and shorts_enabled):
        can_short = False
    elif p.mode == "neutral":
        can_short = True
    else:
        can_short = trend < p.short_regime_max

    # 1) hysteresis on entry/exit
    sel = {}
    for k, v in a.items():
        side = st.active.get(k, 0)
        if side > 0 and v > p.exit:
            sel[k] = v
        elif side < 0 and v < -p.exit and can_short:
            sel[k] = v
        elif v > p.enter:
            sel[k] = v
        elif v < -p.enter and can_short:
            sel[k] = v
    st.active = {k: int(np.sign(v)) for k, v in sel.items()}
    if not sel:
        w = pd.Series(0.0, index=current_w.index.union(alpha.index))
        return _band(w, current_w, nav, p), {"gross": 0.0, "risk_on": risk_on, "dd_mult": 1.0, "n": 0}

    s = pd.Series(sel)
    v = vol.reindex(s.index).replace(0, np.nan).fillna(vol.median())
    raw = s / (v * np.sqrt(24))              # risk-budget: alpha per unit of daily vol
    raw[raw < 0] *= p.short_scale

    # 2) gross budget (optionally regime-scaled) and side construction
    gross_budget = p.max_gross * (max(p.min_risk_on_gross, risk_on) if p.use_regime_gross else 1.0)
    if p.mode == "neutral" and can_short:
        L, Sh = raw[raw > 0], raw[raw < 0]
        if len(L) == 0 or len(Sh) == 0:
            raw = raw * 0.0
        else:
            raw = pd.concat([L / L.sum() * gross_budget / 2, Sh / -Sh.sum() * gross_budget / 2])
    elif raw.abs().sum() > 0:
        raw = raw / raw.abs().sum() * gross_budget

    # 3) covariance-aware vol targeting (only ever scales DOWN to the budget)
    cols = [c for c in raw.index if c in recent_returns.columns]
    rr = recent_returns[cols].tail(p.cov_window).values
    if len(rr) > 48 and len(cols) > 0:
        cov = shrunk_cov(rr, p.cov_shrink) * 24 * 365
        wv = raw[cols].values
        port_vol = float(np.sqrt(max(wv @ cov @ wv, 1e-12)))
        if port_vol > p.target_vol_ann:
            raw = raw * (p.target_vol_ann / port_vol)
    else:
        port_vol = float("nan")

    # 4) hard caps
    caps = pd.Series({k: (p.max_weight_major if k in p.majors else p.max_weight) for k in raw.index})
    raw = raw.clip(-caps, caps)
    if raw.abs().sum() > p.max_gross:
        raw = raw / raw.abs().sum() * p.max_gross

    # 5) drawdown governor
    m = drawdown_multiplier(nav, st.peak_nav, p)
    raw = raw * m

    w = raw.reindex(current_w.index.union(raw.index).union(alpha.index)).fillna(0.0)
    diag = {"gross": float(w.abs().sum()), "net": float(w.sum()), "risk_on": risk_on,
            "mkt_trend": trend, "dd_mult": m, "ex_ante_vol": port_vol, "n": int((w != 0).sum()),
            "can_short": can_short}
    return _band(w, current_w, nav, p), diag


def _band(w: pd.Series, cur: pd.Series, nav: float, p: PolicyParams) -> pd.Series:
    """No-trade band: keep current weight unless the change is material.
    Closing a position entirely (target 0) or flipping sign always passes."""
    cur = cur.reindex(w.index).fillna(0.0)
    dw = w - cur
    thresh = np.maximum(p.band_abs, p.band_rel * w.abs())
    material = (dw.abs() > thresh) | ((w == 0) & (cur != 0)) | (np.sign(w) * np.sign(cur) < 0)
    material &= (dw.abs() * nav >= p.min_trade_usd) | (w == 0)
    return w.where(material, cur)
