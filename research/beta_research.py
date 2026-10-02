"""Low-turnover allocation research (fast, vectorised, net of fees).

Questions:
  1. Does a slow market-trend filter avoid the big crypto bear legs net of cost?
  2. Does volatility targeting improve Sharpe/MaxDD (vol is persistent, returns are not)?
  3. Does cross-sectional selection (top-K by slow vol-normalised trend) add on top?
  4. How fragile are the answers to the lookback choice? (we report a whole grid,
     and only trust regions of parameter space that are uniformly good)

Rebalance every `reb` hours; cost = taker fee x one-way turnover (conservative:
the live bot uses maker orders for most of these trades).

usage: python research/beta_research.py
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from t105 import metrics as M  # noqa: E402
from t105.data.universe import load_panels, price_precisions  # noqa: E402
from t105.strategy.core import tick_bps  # noqa: E402
from t105.strategy.signals import eligibility  # noqa: E402

COST = 0.0010


def simulate(w: pd.DataFrame, r: pd.DataFrame, reb: int, cost: float = COST) -> pd.Series:
    """w: desired weights at each hour (decided at close t). Held from t to t+reb."""
    w = w.fillna(0.0)
    held = w.iloc[::reb].reindex(w.index).ffill().fillna(0)
    # drift between rebalances is ignored for the cost (small); returns use held weights
    pnl = (held.shift(1) * r.fillna(0)).sum(axis=1)
    turn = (held - held.shift(1)).abs().sum(axis=1)
    nav = (1 + pnl - turn * cost).cumprod() * 1e5
    return nav


def stats(name: str, nav: pd.Series) -> dict:
    s = M.summary(nav)
    w = M.rolling_window_stats(nav, 24 * 14, 48)
    return {"variant": name, "ret": s["total_return"], "vol": s["ann_vol"], "sharpe": s["sharpe"],
            "mdd": s["max_drawdown"], "calmar_raw": s["calmar_raw"], "psr": s["psr_vs_0"],
            "w_med_ret": w["ret"].median(), "w_p_pos": (w["ret"] > 0).mean(),
            "w_q10": w["ret"].quantile(0.1), "w_med_mdd": w["mdd"].median(), "w_q90_mdd": w["mdd"].quantile(0.9),
            "w_med_sharpe": w["sharpe"].median()}


def main() -> None:
    P, info = load_panels(ROOT)
    close = P["close"]
    elig = eligibility(close, P["quote_volume"], 24 * 30, 5e6) & (tick_bps(close, price_precisions(info)) <= 5)
    r = close.pct_change(fill_method=None).where(elig.shift(1, fill_value=False))
    lr = np.log1p(r)
    start = close.index[24 * 60]
    rm = r.mean(axis=1)
    idx = np.log1p(rm.fillna(0)).cumsum()
    vol_m = np.sqrt((rm ** 2).ewm(halflife=72, min_periods=48).mean()) * np.sqrt(8760)
    ew = elig.astype(float).div(elig.sum(axis=1), axis=0)
    vol_i = np.sqrt((lr ** 2).ewm(halflife=72, min_periods=48).mean())

    rows = []
    sl = slice(start, None)
    rows.append(stats("EW_hold(daily reb)", simulate(ew.loc[sl], r.loc[sl], 24)))
    btc = pd.DataFrame(0.0, index=close.index, columns=close.columns)
    btc["BTC/USD"] = 1.0
    rows.append(stats("BTC_hold", simulate(btc.loc[sl], r.loc[sl], 24 * 999)))

    for tv in (0.3, 0.5):
        scale = (tv / vol_m).clip(upper=1.0)
        rows.append(stats(f"EW_voltarget{tv}", simulate(ew.mul(scale, axis=0).loc[sl], r.loc[sl], 24)))

    for lb in (168, 336, 504, 720):
        z = (idx - idx.shift(lb))
        on = (z > 0).astype(float)
        rows.append(stats(f"EW_trend{lb}h", simulate(ew.mul(on, axis=0).loc[sl], r.loc[sl], 24)))
        scale = (0.4 / vol_m).clip(upper=1.0)
        rows.append(stats(f"EW_trend{lb}h+vt0.4", simulate(ew.mul(on * scale, axis=0).loc[sl], r.loc[sl], 24)))

    # cross-sectional: top-K by slow vol-normalised trend, inverse-vol weights
    for lb in (168, 336, 720):
        tz = (np.log(close) - np.log(close.shift(lb))) / (vol_i * np.sqrt(lb))
        tz = tz.where(elig)
        for K in (5, 10):
            rk = tz.rank(axis=1, ascending=False)
            pick = (rk <= K) & (tz > 0)
            raw = pick.astype(float) / vol_i
            w = raw.div(raw.sum(axis=1).replace(0, np.nan), axis=0).fillna(0)
            mkt_on = ((idx - idx.shift(336)) > 0).astype(float)
            scale = (0.4 / vol_m).clip(upper=1.0)
            rows.append(stats(f"XS_top{K}_lb{lb}", simulate(w.loc[sl], r.loc[sl], 24)))
            rows.append(stats(f"XS_top{K}_lb{lb}+mkt336+vt", simulate(w.mul(mkt_on * scale, axis=0).loc[sl], r.loc[sl], 24)))

    df = pd.DataFrame(rows)
    pd.set_option("display.width", 250)
    print(df.round(3).to_string(index=False))
    df.to_csv(ROOT / "reports" / "backtest" / "beta_research.csv", index=False)


if __name__ == "__main__":
    main()
