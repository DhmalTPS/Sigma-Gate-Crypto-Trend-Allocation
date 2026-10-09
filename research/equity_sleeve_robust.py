"""R8b: broader offline robustness check of a stock-token sleeve (no downloads).

Adds to R8: other signal families, a parameter sweep, per-token spread costs from the live ticker
snapshot, session timing, sleeve sizes 10/20/30 %, and a stationary-bootstrap CI for the best rule.
Selection bias warning: we test many rules on ~80 days; the best one is expected to look good by chance.

usage: python research/equity_sleeve_robust.py
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

from equity_sleeve import MAJORS, gate_weights, load  # noqa: E402

from t105 import metrics as M  # noqa: E402
from t105.data.universe import TOKENISED_EQUITIES  # noqa: E402

FEE = 0.0010


def run(w: pd.DataFrame, r: pd.DataFrame, hs: pd.Series, reb: int = 24) -> pd.Series:
    """Weights held reb hours; cost = (taker fee + per-token half spread) x turnover."""
    w = w.fillna(0.0)
    held = w.iloc[::reb].reindex(w.index).ffill().fillna(0)
    pnl = (held.shift(1) * r.fillna(0)).sum(axis=1)
    cost = ((held - held.shift(1)).abs() * (FEE + hs.reindex(held.columns).fillna(5e-4))).sum(axis=1)
    return (1 + pnl - cost).cumprod() * 1e5


def st(nav: pd.Series) -> tuple:
    r = nav.pct_change().dropna().values
    return nav.iloc[-1] / nav.iloc[0] - 1, M.sharpe(r), M.max_drawdown(nav.values)


def main() -> None:
    eq = load(sorted(f"{s}/USD" for s in TOKENISED_EQUITIES))
    eq = eq.loc[:, eq.notna().sum() >= 24 * 90]
    cr = load(MAJORS).loc[eq.index[0] :]
    eq = eq.loc[: cr.index[-1]]
    r_eq, r_cr = eq.pct_change(fill_method=None), cr.pct_change(fill_method=None)
    snap = json.loads((ROOT / "data" / "cache" / "ticker_snapshot.json").read_text())["Data"]
    hs = pd.Series(
        {p: (snap[p]["MinAsk"] - snap[p]["MaxBid"]) / (snap[p]["MinAsk"] + snap[p]["MaxBid"]) for p in eq if p in snap}
    )
    print(f"tokens {len(eq.columns)}; median half-spread {hs.median() * 1e4:.1f} bp, max {hs.max() * 1e4:.1f} bp")
    start = eq.index[0] + pd.Timedelta(days=31)
    mid = eq.loc[start:].index[len(eq.loc[start:]) // 2]
    lr = np.log(eq).diff()
    vol = np.sqrt((lr**2).ewm(halflife=72, min_periods=48).mean())
    ew = eq.notna().astype(float).div(eq.notna().sum(axis=1), axis=0)

    rules = {"EW hold": ew}
    for lbs in [(24, 72, 168), (48, 120, 240), (72, 168, 336), (120, 240, 480), (168, 336, 504)]:
        rules[f"gates {lbs[0] // 24}/{lbs[1] // 24}/{lbs[2] // 24}d"] = gate_weights(eq, lbs, 0.20)
    for L in (72, 168, 336):  # cross-sectional momentum, long-only top 4, inverse vol
        z = (np.log(eq) - np.log(eq.shift(L))) / (vol * np.sqrt(L))
        pick = (z.rank(axis=1, ascending=False) <= 4) & (z > 0)
        raw = pick.astype(float) / vol
        rules[f"XS mom top4 {L // 24}d"] = raw.div(raw.sum(axis=1).replace(0, np.nan), axis=0).fillna(0)
    for L in (6, 24):  # cross-sectional reversal, long the 4 biggest losers
        z = (np.log(eq) - np.log(eq.shift(L))) / (vol * np.sqrt(L))
        pick = z.rank(axis=1) <= 4
        raw = pick.astype(float) / vol
        rules[f"XS reversal {L}h"] = raw.div(raw.sum(axis=1).replace(0, np.nan), axis=0).fillna(0)
    h = eq.index.hour + eq.index.minute / 60
    sess = pd.Series(((h >= 13.5) & (h < 20) & (eq.index.dayofweek < 5)).astype(float), index=eq.index)
    rules["EW hold only US session"] = ew.mul(sess, axis=0)
    rules["EW hold only off-session"] = ew.mul(1 - sess, axis=0)

    rows = []
    for name, w in rules.items():
        reb = 1 if "session" in name else 24
        f = st(run(w.loc[start:], r_eq.loc[start:], hs, reb))
        a = st(run(w.loc[start:mid], r_eq.loc[start:mid], hs, reb))
        b = st(run(w.loc[mid:], r_eq.loc[mid:], hs, reb))
        rows.append({"rule": name, "ret": f[0], "sharpe": f[1], "mdd": f[2], "sh_half1": a[1], "sh_half2": b[1]})
    df = pd.DataFrame(rows).set_index("rule").sort_values("sharpe", ascending=False)
    print(df.round(2).to_string())
    print(f"rules tested: {len(df)}; rules positive in BOTH halves: {((df.sh_half1 > 0) & (df.sh_half2 > 0)).sum()}")

    best = df.drop(index="EW hold").index[0]
    nav_b = run(rules[best].loc[start:], r_eq.loc[start:], hs, 1 if "session" in best else 24)
    rb = nav_b.pct_change().dropna().values
    boot = M.stationary_bootstrap(rb, M.sharpe, n_boot=300, mean_block=48)
    print(
        f"best active rule '{best}': Sharpe {M.sharpe(rb):.2f}, bootstrap 90% CI "
        f"[{np.quantile(boot, 0.05):.2f}, {np.quantile(boot, 0.95):.2f}], P(SR>0) {(boot > 0).mean():.2f}"
    )
    sd = df["sharpe"].std()
    print(
        f"expected max Sharpe of {len(df)} zero-skill rules (deflated threshold, ann.): "
        f"{M.deflated_sharpe_threshold(len(df), sd / np.sqrt(8760)) * np.sqrt(8760):.2f}"
    )

    w_cr = gate_weights(cr, (126, 252, 540), 0.30)
    nav_cr = run(w_cr.loc[start:], r_cr.loc[start:], pd.Series(1e-4, index=cr.columns))
    print("sleeve size test (crypto sleeve + share in the best ACTIVE stock rule / in EW hold):")
    for share in (0.0, 0.1, 0.2, 0.3):
        for nm, w in ((best, rules[best]), ("EW hold", ew)):
            nav_e = run(w.loc[start:], r_eq.loc[start:], hs, 1 if "session" in nm else 24)
            mix = (1 - share) * nav_cr + share * nav_e
            s = st(mix)
            print(f"   {share:.0%} in {nm:<26} ret {s[0]:+.2%} sharpe {s[1]:+.2f} maxDD {s[2]:.2%}")
            if share == 0.0:
                break


if __name__ == "__main__":
    main()
