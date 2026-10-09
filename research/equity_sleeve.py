"""v2 research (offline only): would a separate stock-token sleeve help?

Roostoo lists 21 tokenised US stocks (suffix B). We only have ~112 days of hourly history for
them (Binance), so every result here is low-confidence by construction. Questions:
  Q1 diversification: correlation of the stock-token basket with the crypto-majors basket
  Q2 market hours: share of stock-token variance inside the US cash session (13:30-20:00 UTC)
  Q3 does a simple, pre-specified rule work after fees (hold / vol-target / trend gates), in both halves?
  Q4 does 80 % crypto sleeve + 20 % stock sleeve beat the crypto sleeve alone on the same period?
Costs: 10 bp per unit turnover (taker), daily rebalance.

usage: python research/equity_sleeve.py
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

from t105 import metrics as M  # noqa: E402
from t105.data.history import binance_symbol  # noqa: E402
from t105.data.universe import TOKENISED_EQUITIES  # noqa: E402

MAJORS = ["BTC/USD", "ETH/USD", "SOL/USD", "BNB/USD", "XRP/USD"]


def load(pairs: list[str]) -> pd.DataFrame:
    out = {}
    for p in pairs:
        f = ROOT / "data" / "cache" / f"{binance_symbol(p)}_1h.csv.gz"
        if f.exists():
            out[p] = pd.read_csv(f, index_col=0, parse_dates=True)["close"]
    c = pd.DataFrame(out).sort_index()
    return c.reindex(pd.date_range(c.index[0], c.index[-1], freq="1h", tz="UTC")).ffill(limit=3)


def gate_weights(close: pd.DataFrame, lbs: tuple, tv: float, short_scale: float = 0.5) -> pd.DataFrame:
    lr = np.log(close).diff()
    r = close.pct_change(fill_method=None)
    vol = np.sqrt((lr**2).ewm(halflife=72, min_periods=48).mean())
    sig = sum(np.sign(np.log(close) - np.log(close.shift(lb))) for lb in lbs) / len(lbs)
    expo = sig.clip(lower=0) + short_scale * sig.clip(upper=0)
    inv = (1 / vol).where(close.notna())
    base = inv.div(inv.sum(axis=1), axis=0)
    pv = np.sqrt(((base.shift(1) * r).sum(axis=1) ** 2).ewm(halflife=72, min_periods=48).mean()) * np.sqrt(8760)
    return (expo * base).mul((tv / pv).clip(upper=1).fillna(0.5), axis=0).fillna(0)


def stats(nav: pd.Series) -> dict:
    r = nav.pct_change().dropna().values
    return {"ret": nav.iloc[-1] / nav.iloc[0] - 1, "sharpe": M.sharpe(r), "mdd": M.max_drawdown(nav.values)}


def main() -> None:
    eq_pairs = sorted(f"{s}/USD" for s in TOKENISED_EQUITIES)
    eq = load(eq_pairs)
    eq = eq.loc[:, eq.notna().sum() >= 24 * 90]  # >= 90 days of history
    cr = load(MAJORS)
    start = eq.dropna(how="all").index[0] + pd.Timedelta(days=31)  # leave 30d for 720h-free warm-up
    cr, eq = cr.loc[eq.index[0] :], eq.loc[: cr.index[-1]]
    r_eq, r_cr = eq.pct_change(fill_method=None), cr.pct_change(fill_method=None)
    print(f"stock tokens used: {len(eq.columns)}  period {eq.index[0]:%Y-%m-%d} -> {eq.index[-1]:%Y-%m-%d}")

    # Q1 diversification
    b_eq, b_cr = r_eq.mean(axis=1), r_cr.mean(axis=1)
    d_eq, d_cr = (1 + b_eq).resample("1D").prod() - 1, (1 + b_cr).resample("1D").prod() - 1
    print(f"Q1 corr(stock basket, crypto basket): hourly {b_eq.corr(b_cr):+.2f}  daily {d_eq.corr(d_cr):+.2f}")

    # Q2 market hours
    h = r_eq.index.hour + r_eq.index.minute / 60
    sess = (h >= 13.5) & (h < 20) & (r_eq.index.dayofweek < 5)
    var = (r_eq**2).sum(axis=1)
    print(
        f"Q2 share of stock-token variance in US session (27% of hours): {var[sess].sum() / var.sum():.0%}; "
        f"weekend share of variance: {var[r_eq.index.dayofweek >= 5].sum() / var.sum():.0%}"
    )

    # Q3 rules on the stock sleeve, after fees, full period and halves
    mid = eq.loc[start:].index[len(eq.loc[start:]) // 2]
    ew = eq.notna().astype(float).div(eq.notna().sum(axis=1), axis=0)
    rules = {
        "EW hold": ew,
        "trend gates 3/7/14d vt20": gate_weights(eq, (72, 168, 336), 0.20),
        "trend gates 1/3/7d vt20": gate_weights(eq, (24, 72, 168), 0.20),
    }
    rows = []
    for name, w in rules.items():
        for tag, sl in (("full", slice(start, None)), ("half1", slice(start, mid)), ("half2", slice(mid, None))):
            s = stats(simulate(w.loc[sl], r_eq.loc[sl], 24))
            rows.append({"rule": name, "sample": tag, **s})
    t = pd.DataFrame(rows).pivot(index="rule", columns="sample")
    print("Q3 stock sleeve alone (ret / sharpe / mdd):")
    print(t.round(3).to_string())

    # Q4 combine with a crypto sleeve (vectorised stand-in for the live majors trend gates)
    w_cr = gate_weights(cr, (126, 252, 540), 0.30)
    nav_cr = simulate(w_cr.loc[start:], r_cr.loc[start:], 24)
    best = "trend gates 3/7/14d vt20"
    nav_eq = simulate(rules[best].loc[start:], r_eq.loc[start:], 24)
    mix = 0.8 * nav_cr + 0.2 * nav_eq
    print("Q4 same period:")
    for name, nav in (("crypto sleeve only", nav_cr), ("stock sleeve only", nav_eq), ("80/20 mix", mix)):
        s = stats(nav)
        print(f"   {name:<20} ret {s['ret']:+.2%}  sharpe {s['sharpe']:+.2f}  maxDD {s['mdd']:.2%}")


if __name__ == "__main__":
    main()
