"""End-game analysis: which variant maximises P(finish positive) from a -2% start with 10 days left?

Every variant runs through the full execution-aware engine (same code as live, fees, conservative fills).
For each, all 10-day windows (step 24h) are scored:
  P(ret > +2.1%)  -> needed to finish the contest positive from -2.0%
  P(ret > 0), median, q10 (bad case), q90 (good case), worst window, window max-drawdown q90
Also reported conditional on the window STARTING in a drawdown of >= 2% from the 14-day peak
(our live situation), and the full-sample Sharpe / MaxDD for context.

usage: python research/endgame.py
"""

from __future__ import annotations

import sys
import time
from dataclasses import replace
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "research"))

from backtest_variants import half_spreads  # noqa: E402

from t105 import config as CFG  # noqa: E402
from t105 import metrics as M  # noqa: E402
from t105.backtest.engine import run  # noqa: E402
from t105.data.universe import load_panels, price_precisions  # noqa: E402
from t105.strategy.core import compute  # noqa: E402

NEED = 0.021
H = 24 * int(__import__("os").environ.get("DAYS", "10"))


def windows(nav: pd.Series) -> pd.DataFrame:
    rows = []
    v = nav.values
    peak14 = nav.rolling(24 * 14, min_periods=1).max().values
    for s in range(0, len(v) - H, 24):
        seg = v[s : s + H + 1]
        rows.append(
            {
                "ret": seg[-1] / seg[0] - 1,
                "mdd": M.max_drawdown(seg),
                "start_dd": 1 - v[s] / peak14[s],
            }
        )
    return pd.DataFrame(rows)


def main() -> None:
    cfg = CFG.load(ROOT)
    sp, pol, ex = cfg["strategy"], cfg["policy"], cfg["execution"]
    P, info = load_panels(ROOT)
    P = {k: v[list(sp.universe)] for k, v in P.items()}
    hs = half_spreads(P["close"], info)
    start = P["close"].index[24 * 45]
    variants = {
        "A current (vt30)": (sp, pol),
        "B1 vt40": (sp, replace(pol, target_vol_ann=0.40)),
        "B2 vt45": (sp, replace(pol, target_vol_ann=0.45)),
        "C faster gates x0.75": (replace(sp, gate_lookbacks=(126, 252, 540)), pol),
        "D full shorts": (sp, replace(pol, short_scale=1.0)),
        "E loose DD gov (6%->15%)": (sp, replace(pol, dd_soft=0.06, dd_hard=0.15)),
        "F vt40 + loose DD": (sp, replace(pol, target_vol_ann=0.40, dd_soft=0.06, dd_hard=0.15)),
        "G vt40 + full shorts + loose DD": (
            sp,
            replace(pol, target_vol_ann=0.40, short_scale=1.0, dd_soft=0.06, dd_hard=0.15),
        ),
        "H faster gates + loose DD": (
            replace(sp, gate_lookbacks=(126, 252, 540)),
            replace(pol, dd_soft=0.06, dd_hard=0.15),
        ),
        "I faster gates + vt40 + loose DD": (
            replace(sp, gate_lookbacks=(126, 252, 540)),
            replace(pol, target_vol_ann=0.40, dd_soft=0.06, dd_hard=0.15),
        ),
    }
    only = set(sys.argv[1:])
    if only:
        variants = {k: v for k, v in variants.items() if k.split()[0] in only}
    cache = {}
    rows = []
    for name, (spx, polx) in variants.items():
        t0 = time.time()
        key = spx.gate_lookbacks
        if key not in cache:
            cache[key] = compute(P["close"], P["quote_volume"], spx, price_precisions(info))
        res = run(cache[key], P["close"], P["high"], P["low"], polx, ex, start=start, half_spread_bps=hs)
        s = M.summary(res.nav, fees_paid=res.fees)
        w = windows(res.nav)
        dd = w[w["start_dd"] >= 0.02]
        rows.append(
            {
                "variant": name,
                "P(>+2.1%)": (w["ret"] > NEED).mean(),
                "P(>0)": (w["ret"] > 0).mean(),
                "median": w["ret"].median(),
                "q10": w["ret"].quantile(0.1),
                "q90": w["ret"].quantile(0.9),
                "worst": w["ret"].min(),
                "mdd_q90": w["mdd"].quantile(0.9),
                "n_dd": len(dd),
                "P(>2.1%)|dd>=2%": (dd["ret"] > NEED).mean() if len(dd) else np.nan,
                "median|dd>=2%": dd["ret"].median() if len(dd) else np.nan,
                "Sharpe_full": s["sharpe"],
                "MaxDD_full": s["max_drawdown"],
                "fees": res.fees,
            }
        )
        print(f"{name:<34} done ({time.time() - t0:.0f}s)", flush=True)
    df = pd.DataFrame(rows).set_index("variant")
    pd.set_option("display.width", 250)
    fmt = df.copy()
    for c in ["P(>+2.1%)", "P(>0)", "P(>2.1%)|dd>=2%"]:
        fmt[c] = (df[c] * 100).round(0)
    for c in ["median", "q10", "q90", "worst", "mdd_q90", "median|dd>=2%", "MaxDD_full"]:
        fmt[c] = (df[c] * 100).round(2)
    fmt["Sharpe_full"] = df["Sharpe_full"].round(2)
    fmt["fees"] = df["fees"].round(0)
    print(fmt.to_string())
    df.to_csv(ROOT / "reports" / "backtest" / "endgame.csv")


if __name__ == "__main__":
    main()
