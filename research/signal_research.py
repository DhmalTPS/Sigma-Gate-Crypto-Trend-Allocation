"""Signal research: rank-IC term structure of each sleeve, with HAC-style t-stats.

For each sleeve and horizon h, IC_t = Spearman(score_t, fwd_return_{t->t+h}) across
eligible assets. Overlapping horizons make IC_t autocorrelated, so the t-stat uses
non-overlapping subsampling (every h-th observation) -- conservative, but honest.
Also splits by regime (market trend up / down) to see conditional behaviour.

usage: python research/signal_research.py
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from t105.data.universe import load_panels, price_precisions  # noqa: E402
from t105.strategy.core import StrategyParams, compute  # noqa: E402

H = [1, 4, 12, 24, 72, 168]


def ic_series(score: pd.DataFrame, fwd: pd.DataFrame) -> pd.Series:
    a = score.rank(axis=1)
    b = fwd.where(score.notna()).rank(axis=1)
    a = a.where(b.notna())
    am, bm = a.sub(a.mean(axis=1), axis=0), b.sub(b.mean(axis=1), axis=0)
    num = (am * bm).sum(axis=1)
    den = np.sqrt((am ** 2).sum(axis=1) * (bm ** 2).sum(axis=1))
    return (num / den).where(a.notna().sum(axis=1) >= 8)


def tstat_nonoverlap(x: pd.Series, h: int) -> tuple[float, float]:
    x = x.dropna()
    sub = x.iloc[::h]
    return float(x.mean()), float(sub.mean() / sub.std(ddof=1) * np.sqrt(len(sub))) if len(sub) > 5 else np.nan


def main() -> None:
    P, info = load_panels(ROOT)
    close, qv = P["close"], P["quote_volume"]
    p = StrategyParams()
    out = compute(close, qv, p, price_precisions(info))
    print(f"panel {close.shape}, eligible avg {out.eligible.sum(axis=1).mean():.1f} assets")
    logp = np.log(close)
    reg = out.regime["mkt_trend"]
    rows = []
    sleeves = dict(out.sleeves, combined=out.alpha)
    for name, sc in sleeves.items():
        for h in H:
            fwd = logp.shift(-h) - logp
            ic = ic_series(sc, fwd)
            m, t = tstat_nonoverlap(ic, h)
            up = tstat_nonoverlap(ic[reg > 0.2], h)[0]
            dn = tstat_nonoverlap(ic[reg < -0.2], h)[0]
            first, second = tstat_nonoverlap(ic.iloc[: len(ic) // 2], h)[0], tstat_nonoverlap(ic.iloc[len(ic) // 2:], h)[0]
            rows.append({"sleeve": name, "h": h, "IC": m, "t": t, "IC_up": up, "IC_down": dn,
                         "IC_1st_half": first, "IC_2nd_half": second})
    df = pd.DataFrame(rows)
    pd.set_option("display.width", 160)
    print(df.round(4).to_string(index=False))

    # Time-series predictive power of the regime/trend (directional, market level)
    rm = out.returns.where(out.eligible).mean(axis=1)
    print("\nmarket-level: corr(mkt_trend_t, EW market fwd return)")
    for h in (4, 12, 24, 72):
        fwd = np.log1p(rm).rolling(h).sum().shift(-h)
        c = pd.concat([reg, fwd], axis=1).dropna().iloc[::h].corr().iloc[0, 1]
        print(f"  h={h:>3}  corr={c:+.4f}")
    df.to_csv(ROOT / "reports" / "backtest" / "signal_ic.csv", index=False)
    print("\nsleeve weights (mean):", out.sleeve_weights.mean().round(3).to_dict())


if __name__ == "__main__":
    main()
