"""Compare portfolio constructions net of competition fees.

Each variant is scored on (a) the full sample and (b) the *distribution* of
competition-length outcomes: every 14-day window (stepped by 2 days) is scored
as if it were the live contest. We care about median return, P(return > 0),
median MaxDD and median composite -- not the single best backtest number.

usage: python research/backtest_variants.py [--quick]
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import replace
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from t105 import metrics as M  # noqa: E402
from t105.backtest.engine import ExecParams, run  # noqa: E402
from t105.data.universe import load_panels, price_precisions  # noqa: E402
from t105.portfolio.policy import PolicyParams  # noqa: E402
from t105.strategy.core import StrategyParams, compute  # noqa: E402


def half_spreads(close: pd.DataFrame, info: dict) -> pd.Series:
    snap = json.loads((ROOT / "data" / "cache" / "ticker_snapshot.json").read_text())["Data"]
    out = {}
    for p in close.columns:
        d = snap.get(p)
        if d and d["MaxBid"] and d["MinAsk"]:
            mid = (d["MaxBid"] + d["MinAsk"]) / 2
            out[p] = max(0.5, (d["MinAsk"] - d["MaxBid"]) / 2 / mid * 1e4)
    return pd.Series(out)


def window_table(nav: pd.Series) -> dict:
    w = M.rolling_window_stats(nav, 24 * 14, 48)
    return {"win_med_ret": w["ret"].median(), "win_p_pos": (w["ret"] > 0).mean(),
            "win_q10_ret": w["ret"].quantile(0.1), "win_med_mdd": w["mdd"].median(),
            "win_q90_mdd": w["mdd"].quantile(0.9), "win_med_comp": w["composite"].median(),
            "win_med_sharpe": w["sharpe"].median()}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default=None)
    a = ap.parse_args()
    P, info = load_panels(ROOT)
    close = P["close"]
    hs = half_spreads(close, info)
    pp = price_precisions(info)
    start = pd.Timestamp(a.start, tz="UTC") if a.start else close.index[24 * 45]

    rows = []
    # ---------- benchmarks
    r = close.pct_change(fill_method=None)
    elig_ew = r.loc[start:].mean(axis=1).fillna(0)
    ew_nav = (1 + elig_ew).cumprod() * 1e5
    btc_nav = close["BTC/USD"].loc[start:] / close["BTC/USD"].loc[start] * 1e5
    for name, nav in (("bench_EW_hold", ew_nav), ("bench_BTC_hold", btc_nav)):
        rows.append({"variant": name, **M.summary(nav), **window_table(nav)})

    strat_sets = {
        "rev6+trend": StrategyParams(sleeves=("trend", "reversal"), prior_weights={"trend": 0.4, "reversal": 0.6}),
        "rev12+trend": StrategyParams(sleeves=("trend", "reversal"), reversal_lookback=12,
                                      prior_weights={"trend": 0.4, "reversal": 0.6}),
        "rev6_only": StrategyParams(sleeves=("reversal",), prior_weights={"reversal": 1.0}),
        "trend_only": StrategyParams(sleeves=("trend",), prior_weights={"trend": 1.0}),
    }
    pols = {
        "LO": PolicyParams(mode="long_only"),
        "NEU": PolicyParams(mode="neutral"),
        "LS": PolicyParams(mode="long_short"),
    }
    ex = ExecParams()
    cache = {}
    for sname, sp in strat_sets.items():
        t0 = time.time()
        out = compute(close, P["quote_volume"], sp, pp)
        cache[sname] = out
        for pname, pol in pols.items():
            if sname == "trend_only" and pname != "LO":
                continue
            res = run(out, close, P["high"], P["low"], pol, ex, start=start, half_spread_bps=hs)
            row = {"variant": f"{sname}|{pname}",
                   **M.summary(res.nav, fees_paid=res.fees, turnover=res.turnover, n_trades=len(res.trades)),
                   **window_table(res.nav), "maker_ratio": res.maker_ratio}
            rows.append(row)
            print(f"{row['variant']:<22} ret {row['total_return']:+.3f} sh {row['sharpe']:.2f} "
                  f"mdd {row['max_drawdown']:.3f} turn {row['turnover_x_nav']:.0f}x fees {row['fees_paid']:.0f} "
                  f"maker {res.maker_ratio:.2f} | win med {row['win_med_ret']:+.4f} p+ {row['win_p_pos']:.2f} "
                  f"mdd {row['win_med_mdd']:.3f}  ({time.time()-t0:.0f}s)", flush=True)
    df = pd.DataFrame(rows)
    pd.set_option("display.width", 220)
    cols = ["variant", "total_return", "sharpe", "sortino", "max_drawdown", "calmar_raw", "psr_vs_0",
            "turnover_x_nav", "fees_paid", "win_med_ret", "win_p_pos", "win_q10_ret", "win_med_mdd",
            "win_q90_mdd", "win_med_sharpe"]
    print(df[cols].round(3).to_string(index=False))
    df.to_csv(ROOT / "reports" / "backtest" / "variants.csv", index=False)


if __name__ == "__main__":
    main()
