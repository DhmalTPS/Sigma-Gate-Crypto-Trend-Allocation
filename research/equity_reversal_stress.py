"""R8c: stress tests for the stock-token 6h reversal candidate (offline).

Real effect or fluke? Checks: rebalance-hour sensitivity (24 offsets), lookback/top-K grid, double costs,
weekends excluded, excess over equal-weight hold, and leave-one-token-out.

usage: python research/equity_reversal_stress.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "research"))

from equity_sleeve import MAJORS, load  # noqa: E402

from t105 import metrics as M  # noqa: E402
from t105.data.universe import TOKENISED_EQUITIES  # noqa: E402


def sim(w, r, hs, reb, off, fee):
    w = w.fillna(0.0)
    held = w.iloc[off::reb].reindex(w.index).ffill().fillna(0)
    pnl = (held.shift(1) * r.fillna(0)).sum(axis=1)
    cost = ((held - held.shift(1)).abs() * (fee + hs.reindex(held.columns).fillna(5e-4))).sum(axis=1)
    return pnl - cost


def sh(x):
    return M.sharpe(np.asarray(x.dropna()))


def rev_w(eq, vol, L, K):
    z = (np.log(eq) - np.log(eq.shift(L))) / (vol * np.sqrt(L))
    pick = z.rank(axis=1) <= K
    raw = pick.astype(float) / vol
    return raw.div(raw.sum(axis=1).replace(0, np.nan), axis=0).fillna(0)


def main():
    eq = load(sorted(f"{s}/USD" for s in TOKENISED_EQUITIES))
    eq = eq.loc[:, eq.notna().sum() >= 24 * 90]
    eq = eq.loc[: load(MAJORS).index[-1]]
    r = eq.pct_change(fill_method=None)
    snap = json.loads((ROOT / "data" / "cache" / "ticker_snapshot.json").read_text())["Data"]
    hs = pd.Series({p: (snap[p]["MinAsk"] - snap[p]["MaxBid"]) / (snap[p]["MinAsk"] + snap[p]["MaxBid"]) for p in eq})
    start = eq.index[0] + pd.Timedelta(days=31)
    lr = np.log(eq).diff()
    vol = np.sqrt((lr**2).ewm(halflife=72, min_periods=48).mean())
    ew = eq.notna().astype(float).div(eq.notna().sum(axis=1), axis=0)
    base = rev_w(eq, vol, 6, 4)
    S = slice(start, None)

    offs = [sh(sim(base.loc[S], r.loc[S], hs, 24, o, 0.001)) for o in range(24)]
    print(
        f"1) rebalance hour (24 offsets): Sharpe min {min(offs):.2f} median {np.median(offs):.2f} max {max(offs):.2f}; "
        f"positive in {sum(o > 0 for o in offs)}/24"
    )
    print("2) lookback x topK grid (Sharpe, offset 0):")
    for L in (3, 6, 12, 24):
        print(
            "   L=%2dh " % L
            + "  ".join(
                f"K{K}:{sh(sim(rev_w(eq, vol, L, K).loc[S], r.loc[S], hs, 24, 0, 0.001)):+.2f}" for K in (2, 3, 4, 5, 6)
            )
        )
    print(f"3) double costs (20 bp fee): Sharpe {sh(sim(base.loc[S], r.loc[S], hs * 2, 24, 0, 0.002)):.2f}")
    wk = eq.index.dayofweek < 5
    r_wd = r.where(pd.Series(wk, index=eq.index), 0.0, axis=0)
    print(f"4) weekend returns zeroed: Sharpe {sh(sim(base.loc[S], r_wd.loc[S], hs, 24, 0, 0.001)):.2f}")
    ex = sim(base.loc[S], r.loc[S], hs, 24, 0, 0.001) - sim(ew.loc[S], r.loc[S], hs, 24, 0, 0.001)
    print(f"5) excess over equal-weight hold: ann. {ex.mean() * 8760:+.1%}, Sharpe of excess {sh(ex):.2f}")
    loo = []
    for c in eq.columns:
        e2 = eq.drop(columns=c)
        loo.append(
            (c, sh(sim(rev_w(e2, vol.drop(columns=c), 6, 4).loc[S], r.drop(columns=c).loc[S], hs, 24, 0, 0.001)))
        )
    loo.sort(key=lambda x: x[1])
    print(
        "6) leave-one-token-out Sharpe: worst "
        + ", ".join(f"{c.split('/')[0]} {v:.2f}" for c, v in loo[:3])
        + f" | best {loo[-1][0].split('/')[0]} {loo[-1][1]:.2f}"
    )


if __name__ == "__main__":
    main()
