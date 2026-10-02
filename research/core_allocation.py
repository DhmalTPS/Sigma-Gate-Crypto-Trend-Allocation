"""Majors-centred, vol-targeted core allocation with per-asset trend gates.

Candidates (all rebalanced daily, cost 10 bps x turnover, all vol-targeted):
  MAJ_hold      inverse-vol basket of majors, always invested
  MAJ_gate{lb}  same, but each major held only while its own vol-normalised trend over lb > 0
  MAJ_soft{lb}  continuous exposure: w ~ max(0, tanh(z/1.5)) (no hard on/off whipsaw)
Reported on full sample and on each half (robustness), plus 14-day window distribution.

usage: python research/core_allocation.py
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "research"))

from beta_research import simulate, stats  # noqa: E402
from t105.data.universe import load_panels  # noqa: E402

MAJORS = ["BTC/USD", "ETH/USD", "SOL/USD", "BNB/USD", "XRP/USD"]


def main() -> None:
    P, _ = load_panels(ROOT)
    close = P["close"][MAJORS]
    lr = np.log(close).diff()
    r = close.pct_change(fill_method=None)
    vol = np.sqrt((lr ** 2).ewm(halflife=72, min_periods=48).mean())
    start = close.index[24 * 60]
    mid = close.index[(len(close) + 24 * 60) // 2]
    inv = (1 / vol).div((1 / vol).sum(axis=1), axis=0)

    def vt(w, tv):
        cov_vol = np.sqrt(((w.shift(1) * r).sum(axis=1) ** 2).ewm(halflife=72, min_periods=48).mean()) * np.sqrt(8760)
        # ex-ante approx: scale by realised vol of the same weights (lagged -> causal)
        scale = (tv / cov_vol.replace(0, np.nan)).clip(upper=1.0).fillna(0.5)
        return w.mul(scale, axis=0)

    cands = {"MAJ_hold": inv}
    for lb in (72, 168, 336, 720):
        z = (np.log(close) - np.log(close.shift(lb))) / (vol * np.sqrt(lb))
        cands[f"MAJ_gate{lb}"] = inv * (z > 0)
        cands[f"MAJ_soft{lb}"] = inv * np.tanh(z.clip(lower=0) / 1.5) * 1.3
    rows = []
    for tv in (0.25, 0.40):
        for name, w in cands.items():
            wv = vt(w.clip(upper=1.0), tv)
            for tag, sl in (("full", slice(start, None)), ("h1", slice(start, mid)), ("h2", slice(mid, None))):
                nav = simulate(wv.loc[sl], r.loc[sl], 24)
                s = stats(f"{name}|vt{tv}|{tag}", nav)
                rows.append(s)
    df = pd.DataFrame(rows)
    pd.set_option("display.width", 250)
    print(df[["variant", "ret", "vol", "sharpe", "mdd", "calmar_raw", "psr", "w_med_ret", "w_p_pos", "w_q10",
              "w_med_mdd", "w_q90_mdd"]].round(3).to_string(index=False))
    df.to_csv(ROOT / "reports" / "backtest" / "core_allocation.csv", index=False)


if __name__ == "__main__":
    main()
