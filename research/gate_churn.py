"""Full-engine check of gate churn: dead-band width x decision frequency.

usage: python research/gate_churn.py
"""
from __future__ import annotations

import sys
import time
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "research"))

from backtest_variants import half_spreads  # noqa: E402
from t105 import config as CFG  # noqa: E402
from t105 import metrics as M  # noqa: E402
from t105.backtest.engine import run  # noqa: E402
from t105.data.universe import load_panels, price_precisions  # noqa: E402
from t105.strategy.core import compute  # noqa: E402

cfg = CFG.load(ROOT)
sp, pol, ex = cfg["strategy"], cfg["policy"], cfg["execution"]
P, info = load_panels(ROOT)
P = {k: v[list(sp.universe)] for k, v in P.items()}
hs = half_spreads(P["close"], info)
start = P["close"].index[24 * 45]
for db in (0.0, 0.25, 0.5, 1.0):
    out = compute(P["close"], P["quote_volume"], replace(sp, gate_dead_band=db), price_precisions(info))
    for every in (1, 24):
        t0 = time.time()
        r = run(out, P["close"], P["high"], P["low"], pol, replace(ex, decision_every=every), start=start,
                half_spread_bps=hs)
        s = M.summary(r.nav, fees_paid=r.fees, turnover=r.turnover)
        w = M.rolling_window_stats(r.nav, 24 * 14, 48)
        print(f"db={db:<4} every={every:<3} ret {s['total_return']:+.3f} vol {s['ann_vol']:.3f} sh {s['sharpe']:.2f} "
              f"mdd {s['max_drawdown']:.3f} psr {s['psr_vs_0']:.2f} turn {s['turnover_x_nav']:.1f}x fees {s['fees_paid']:.0f} "
              f"maker {r.maker_ratio:.2f} gross {r.diag['gross'].mean():.2f} | w14 med {w['ret'].median():+.4f} "
              f"p+ {(w['ret']>0).mean():.2f} q90mdd {w['mdd'].quantile(.9):.3f} ({time.time()-t0:.0f}s)", flush=True)
