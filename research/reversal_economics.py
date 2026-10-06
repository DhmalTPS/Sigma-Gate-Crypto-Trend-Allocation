"""Can short-term residual reversal pay for its turnover?

Long-only tilt: hold the K most-oversold eligible coins (residual return over L hours,
vol-normalised), equal risk, rebalanced every H hours. Compared with the EW basket
rebalanced on the same clock, so the difference isolates the selection alpha from beta.
Reported net at maker (5 bps) and taker (10 bps) costs, and split by sample halves.

usage: python research/reversal_economics.py
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "research"))

from beta_research import simulate  # noqa: E402

from t105.data.universe import load_panels, price_precisions  # noqa: E402
from t105.strategy import signals as S  # noqa: E402
from t105.strategy.core import tick_bps  # noqa: E402


def main() -> None:
    P, info = load_panels(ROOT)
    close = P["close"]
    elig = S.eligibility(close, P["quote_volume"], 24 * 30, 5e6) & (tick_bps(close, price_precisions(info)) <= 5)
    lr = np.log(close).diff()
    r = close.pct_change(fill_method=None).where(elig.shift(1, fill_value=False))
    vol = S.ewm_vol(lr)
    rm = S.market_return(lr, elig)
    resid = lr - S.rolling_beta(lr, rm).mul(rm, axis=0)
    start = close.index[24 * 60]
    mid = close.index[len(close) // 2 + 24 * 30]
    ew = elig.astype(float).div(elig.sum(axis=1), axis=0)
    rows = []
    for L in (3, 6, 12, 24):
        z = resid.rolling(L).sum() / (vol * np.sqrt(L))
        z = z.where(elig)
        rk = z.rank(axis=1)  # most negative = rank 1 (most oversold)
        for K in (5, 10):
            pick = (rk <= K).astype(float) / vol
            w = pick.div(pick.sum(axis=1).replace(0, np.nan), axis=0).fillna(0)
            for H in (1, 4, 8, 24):
                for c in (0.0005, 0.0010):
                    nav = simulate(w.loc[start:], r.loc[start:], H, c)
                    bench = simulate(ew.loc[start:], r.loc[start:], H, c)
                    ex = np.log(nav).diff() - np.log(bench).diff()
                    ann = ex.mean() * 8760
                    t = ex.mean() / ex.std() * np.sqrt(len(ex.dropna()))
                    h1, h2 = ex.loc[:mid].mean() * 8760, ex.loc[mid:].mean() * 8760
                    turn = (w.iloc[::H].diff().abs().sum(axis=1)).mean() * (8760 / H)
                    rows.append(
                        {
                            "L": L,
                            "K": K,
                            "H": H,
                            "cost_bps": c * 1e4,
                            "excess_ann": ann,
                            "t": t,
                            "h1": h1,
                            "h2": h2,
                            "turn_per_yr": turn,
                        }
                    )
    df = pd.DataFrame(rows)
    pd.set_option("display.width", 200)
    print(df.round(3).to_string(index=False))
    df.to_csv(ROOT / "reports" / "backtest" / "reversal_economics.csv", index=False)


if __name__ == "__main__":
    main()
