"""Backtest the production config with the live code path, plus robustness diagnostics.

Outputs reports/backtest/<version>/: nav.csv, trades.csv, summary.json, windows.csv,
perturbation.csv and prints a human-readable report.

usage: python scripts/run_backtest.py [--config config/strategy.yaml] [--no-perturb]
"""
from __future__ import annotations

import argparse
import json
import sys
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


def one(P, info, sp, pol, ex, start, hs):
    out = compute(P["close"], P["quote_volume"], sp, price_precisions(info))
    return run(out, P["close"], P["high"], P["low"], pol, ex, start=start, half_spread_bps=hs)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config/strategy.yaml")
    ap.add_argument("--no-perturb", action="store_true")
    a = ap.parse_args()
    cfg = CFG.load(ROOT, a.config)
    sp, pol, ex = cfg["strategy"], cfg["policy"], cfg["execution"]
    P, info = load_panels(ROOT)
    cols = list(sp.universe) if sp.universe else list(P["close"].columns)
    P = {k: v[cols] for k, v in P.items()}
    hs = half_spreads(P["close"], info)
    start = P["close"].index[24 * 45]
    out_dir = ROOT / "reports" / "backtest" / cfg["version"]
    out_dir.mkdir(parents=True, exist_ok=True)

    res = one(P, info, sp, pol, ex, start, hs)
    nav = res.nav
    s = M.summary(nav, fees_paid=res.fees, turnover=res.turnover, n_trades=len(res.trades))
    s["maker_ratio"] = res.maker_ratio
    r = M.returns_from_nav(nav).values
    boot_sh = M.stationary_bootstrap(r, M.sharpe, n_boot=400, mean_block=48)
    s["sharpe_ci90"] = [float(np.quantile(boot_sh, 0.05)), float(np.quantile(boot_sh, 0.95))]
    s["p_sharpe_gt0_boot"] = float((boot_sh > 0).mean())
    win = M.rolling_window_stats(nav, 24 * 14, 24)
    s["window14"] = {"n": len(win), "median_ret": win["ret"].median(), "p_pos": float((win["ret"] > 0).mean()),
                     "q10_ret": win["ret"].quantile(0.1), "q90_ret": win["ret"].quantile(0.9),
                     "median_mdd": win["mdd"].median(), "q90_mdd": win["mdd"].quantile(0.9),
                     "max_mdd": win["mdd"].max(), "median_composite": win["composite"].median()}
    # active-day check: fraction of UTC days with at least one fill
    days = pd.Series(1, index=pd.to_datetime(res.trades["t"])).resample("1D").sum() if len(res.trades) else pd.Series()
    all_days = pd.date_range(nav.index[0].floor("D"), nav.index[-1].floor("D"), freq="1D")
    s["frac_days_with_trades"] = float((days.reindex(all_days).fillna(0) > 0).mean()) if len(days) else 0.0
    # benchmark
    bench_w = pd.Series(1 / len(cols), index=cols)
    rb = P["close"].pct_change(fill_method=None).loc[start:].fillna(0) @ bench_w
    bnav = (1 + rb).cumprod() * 1e5
    s["benchmark_EW_majors_hold"] = M.summary(bnav)

    nav.to_csv(out_dir / "nav.csv")
    res.trades.to_csv(out_dir / "trades.csv", index=False)
    win.to_csv(out_dir / "windows.csv", index=False)
    (out_dir / "summary.json").write_text(json.dumps(s, indent=1, default=float))

    print(f"=== {cfg['version']}  {nav.index[0]:%Y-%m-%d} -> {nav.index[-1]:%Y-%m-%d} ===")
    for k in ("total_return", "ann_vol", "sharpe", "sortino", "max_drawdown", "calmar_raw", "psr_vs_0",
              "p_sharpe_gt0_boot", "fees_paid", "turnover_x_nav", "n_trades", "maker_ratio",
              "frac_days_with_trades"):
        print(f"  {k:<24} {s[k]:.4f}" if isinstance(s[k], float) else f"  {k:<24} {s[k]}")
    print(f"  sharpe 90% CI            [{s['sharpe_ci90'][0]:.2f}, {s['sharpe_ci90'][1]:.2f}]")
    print("  14-day windows:", {k: round(v, 4) for k, v in s["window14"].items()})
    b = s["benchmark_EW_majors_hold"]
    print(f"  benchmark EW majors hold: ret {b['total_return']:.3f} sharpe {b['sharpe']:.2f} mdd {b['max_drawdown']:.3f}")
    if res.diag is not None and len(res.diag):
        print("  avg gross %.2f  avg net %.2f" % (res.diag["gross"].mean(), res.diag["net"].mean()))

    if a.no_perturb:
        return
    # fragility: perturb each key parameter and check the result does not fall off a cliff
    rows = []
    perturb = {
        "gate_lookbacks": [("x0.75", replace(sp, gate_lookbacks=tuple(int(x * 0.75) for x in sp.gate_lookbacks))),
                           ("x1.25", replace(sp, gate_lookbacks=tuple(int(x * 1.25) for x in sp.gate_lookbacks)))],
        "vol_halflife": [("36", replace(sp, vol_halflife=36)), ("144", replace(sp, vol_halflife=144))],
    }
    for name, lst in perturb.items():
        for tag, spx in lst:
            rr = one(P, info, spx, pol, ex, start, hs)
            ss = M.summary(rr.nav, fees_paid=rr.fees)
            rows.append({"param": name, "value": tag, "ret": ss["total_return"], "sharpe": ss["sharpe"],
                         "mdd": ss["max_drawdown"], "fees": rr.fees})
    for name, vals in {"target_vol_ann": [0.2, 0.4], "short_scale": [0.0, 1.0], "band_abs": [0.015, 0.06],
                       "dd_soft": [0.02, 0.05]}.items():
        for v in vals:
            rr = one(P, info, sp, replace(pol, **{name: v}), ex, start, hs)
            ss = M.summary(rr.nav, fees_paid=rr.fees)
            rows.append({"param": name, "value": v, "ret": ss["total_return"], "sharpe": ss["sharpe"],
                         "mdd": ss["max_drawdown"], "fees": rr.fees})
    for v in (False,):
        rr = one(P, info, sp, pol, replace(ex, use_maker=v), start, hs)
        ss = M.summary(rr.nav, fees_paid=rr.fees)
        rows.append({"param": "use_maker", "value": v, "ret": ss["total_return"], "sharpe": ss["sharpe"],
                     "mdd": ss["max_drawdown"], "fees": rr.fees})
    df = pd.DataFrame(rows)
    df.to_csv(out_dir / "perturbation.csv", index=False)
    print("\n  parameter perturbation (base sharpe %.2f):" % s["sharpe"])
    print(df.round(4).to_string(index=False))


if __name__ == "__main__":
    main()
