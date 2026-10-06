"""Final candidate: trend-gate ENSEMBLE (no single tuned lookback), vol-targeted.

exposure_i = mean_{lb in LB} 1[z_i,lb > 0]   (z = vol-normalised log return over lb)
w_i ~ exposure_i / vol_i, portfolio scaled to target vol (lagged realised, causal).
Universes: 5 majors vs top-10 liquid. Robustness: halves, thirds, +/-25% lookbacks.

usage: python research/trend_ensemble.py
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

MAJ5 = ["BTC/USD", "ETH/USD", "SOL/USD", "BNB/USD", "XRP/USD"]
LIQ10 = MAJ5 + ["DOGE/USD", "ADA/USD", "LINK/USD", "AVAX/USD", "LTC/USD"]


def build(close, lbs, tv):
    lr = np.log(close).diff()
    r = close.pct_change(fill_method=None)
    vol = np.sqrt((lr**2).ewm(halflife=72, min_periods=48).mean())
    expo = sum(((np.log(close) - np.log(close.shift(lb))) > 0).astype(float) for lb in lbs) / len(lbs)
    raw = expo / vol
    w = raw.div((1 / vol).sum(axis=1), axis=0)  # fully-invested when every gate is on
    full_w = (1 / vol).div((1 / vol).sum(axis=1), axis=0)
    pv_full = np.sqrt(((full_w.shift(1) * r).sum(axis=1) ** 2).ewm(halflife=72, min_periods=48).mean()) * np.sqrt(8760)
    scale = (tv / pv_full.replace(0, np.nan)).clip(upper=1.0).fillna(0.5)  # vol of the *invested* basket
    return w.mul(scale, axis=0).clip(upper=0.4), r


def main() -> None:
    P, _ = load_panels(ROOT)
    rows = []
    for uname, uni in (("MAJ5", MAJ5), ("LIQ10", LIQ10)):
        close = P["close"][uni]
        start = close.index[24 * 60]
        n = len(close.loc[start:])
        cuts = [close.loc[start:].index[int(n * k / 3)] for k in range(3)] + [close.index[-1]]
        for lbs_name, lbs in (
            ("ens168-336-720", (168, 336, 720)),
            ("ens-25%", (126, 252, 540)),
            ("ens+25%", (210, 420, 900)),
            ("only720", (720,)),
            ("ens72-720", (72, 168, 336, 720)),
        ):
            for tv in (0.25, 0.30, 0.35):
                w, r = build(close, lbs, tv)
                nav = simulate(w.loc[start:], r.loc[start:], 24)
                s = stats(f"{uname}|{lbs_name}|vt{tv}", nav)
                thirds = []
                for a, b in zip(cuts[:-1], cuts[1:]):
                    seg = simulate(w.loc[a:b], r.loc[a:b], 24)
                    rr = seg.pct_change().dropna()
                    thirds.append(rr.mean() / rr.std() * np.sqrt(8760))
                s.update({"sh_T1": thirds[0], "sh_T2": thirds[1], "sh_T3": thirds[2]})
                rows.append(s)
    df = pd.DataFrame(rows)
    pd.set_option("display.width", 250)
    print(
        df[
            [
                "variant",
                "ret",
                "vol",
                "sharpe",
                "mdd",
                "psr",
                "sh_T1",
                "sh_T2",
                "sh_T3",
                "w_med_ret",
                "w_p_pos",
                "w_q10",
                "w_med_mdd",
                "w_q90_mdd",
            ]
        ]
        .round(3)
        .to_string(index=False)
    )
    df.to_csv(ROOT / "reports" / "backtest" / "trend_ensemble.csv", index=False)


if __name__ == "__main__":
    main()
