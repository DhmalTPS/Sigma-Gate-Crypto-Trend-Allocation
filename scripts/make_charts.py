"""Render README figures from committed backtest reports (dev tool, needs matplotlib).

  docs/img/equity_curve.png   strategy vs equal-weight majors buy & hold: NAV and drawdown
  docs/img/robustness.png     Sharpe under one-at-a-time parameter perturbations
  docs/img/live_nav.png       live contest NAV (only if reports/live/nav_hourly.csv exists)

usage: python scripts/make_charts.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import pandas as pd  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
VERSION = "v1.1-gate-deadband"
REP = ROOT / "reports" / "backtest" / VERSION
OUT = ROOT / "docs" / "img"

# Validated categorical palette (scripts/validate_palette: CVD dE 24.7, normal dE 33.6, contrast >= 3:1)
SURFACE, INK, INK2, GRID = "#fcfcfb", "#0b0b0b", "#52514e", "#e6e5e0"
S1, S2 = "#2a78d6", "#eb6834"  # strategy, benchmark


def style(ax, title=None):
    ax.set_facecolor(SURFACE)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color(GRID)
    ax.tick_params(colors=INK2, labelsize=9)
    ax.grid(axis="y", color=GRID, linewidth=0.8)
    ax.set_axisbelow(True)
    if title:
        ax.set_title(title, loc="left", color=INK, fontsize=11, fontweight="bold", pad=10)


def end_label(ax, x, y, text, color):
    ax.annotate(
        text, (x, y), xytext=(6, 0), textcoords="offset points", va="center", fontsize=9, color=INK, fontweight="bold"
    )
    ax.plot([x], [y], "o", color=color, markersize=6, markeredgecolor=SURFACE, markeredgewidth=2)


def benchmark(nav: pd.Series) -> pd.Series:
    from t105.data.universe import load_panels

    P, _ = load_panels(ROOT)
    uni = ["BTC/USD", "ETH/USD", "SOL/USD", "BNB/USD", "XRP/USD"]
    r = P["close"][uni].pct_change(fill_method=None).loc[nav.index[0] :].fillna(0).mean(axis=1)
    return (1 + r).cumprod() * nav.iloc[0]


def equity_curve() -> None:
    nav = pd.read_csv(REP / "nav.csv", index_col=0, parse_dates=True).iloc[:, 0]
    bench = benchmark(nav).reindex(nav.index).ffill()
    s = json.loads((REP / "summary.json").read_text())
    fig, (a1, a2) = plt.subplots(2, 1, figsize=(10, 6.2), sharex=True, height_ratios=[2.2, 1], facecolor=SURFACE)
    style(a1, "Sigma Gate v1.1 vs. equal-weight majors buy & hold (net of all fees)")
    a1.plot(nav.index, nav / 1e3, color=S1, linewidth=2, label="Sigma Gate (trend gates, 30% vol target)")
    a1.plot(bench.index, bench / 1e3, color=S2, linewidth=2, label="Buy & hold BTC/ETH/SOL/BNB/XRP")
    end_label(a1, nav.index[-1], nav.iloc[-1] / 1e3, f"{s['total_return']:+.1%}", S1)
    end_label(a1, bench.index[-1], bench.iloc[-1] / 1e3, f"{s['benchmark_EW_majors_hold']['total_return']:+.1%}", S2)
    a1.set_ylabel("Portfolio value ($k)", color=INK2, fontsize=9)
    a1.legend(loc="lower left", frameon=False, fontsize=9, labelcolor=INK)
    dd_s = (nav / nav.cummax() - 1) * 100
    dd_b = (bench / bench.cummax() - 1) * 100
    style(a2)
    a2.plot(dd_b.index, dd_b, color=S2, linewidth=2)
    a2.plot(dd_s.index, dd_s, color=S1, linewidth=2)
    a2.set_ylabel("Drawdown (%)", color=INK2, fontsize=9)
    a2.annotate(
        f"max {dd_s.min():.1f}%",
        (dd_s.idxmin(), dd_s.min()),
        xytext=(6, -4),
        textcoords="offset points",
        fontsize=9,
        color=INK,
        va="top",
        bbox=dict(boxstyle="round,pad=0.2", facecolor=SURFACE, edgecolor="none"),
    )
    a2.annotate(
        f"max {dd_b.min():.1f}%",
        (dd_b.idxmin(), dd_b.min()),
        xytext=(-70, 26),
        textcoords="offset points",
        fontsize=9,
        color=INK,
        bbox=dict(boxstyle="round,pad=0.2", facecolor=SURFACE, edgecolor="none"),
    )
    fig.text(
        0.01,
        0.005,
        f"Hourly backtest {nav.index[0]:%Y-%m-%d} to {nav.index[-1]:%Y-%m-%d}; Sharpe "
        f"{s['sharpe']:.2f} vs {s['benchmark_EW_majors_hold']['sharpe']:.2f}. "
        "Source: reports/backtest/" + VERSION,
        fontsize=8,
        color=INK2,
    )
    fig.tight_layout(rect=(0, 0.02, 1, 1))
    fig.savefig(OUT / "equity_curve.png", dpi=160, facecolor=SURFACE)
    plt.close(fig)


def robustness() -> None:
    df = pd.read_csv(REP / "perturbation.csv")
    base = json.loads((REP / "summary.json").read_text())["sharpe"]
    names = {
        "gate_lookbacks": "gate lookbacks",
        "vol_halflife": "vol half-life (h)",
        "target_vol_ann": "vol target",
        "short_scale": "short size",
        "band_abs": "no-trade band",
        "dd_soft": "DD soft trigger",
        "use_maker": "maker-first execution",
    }
    df["label"] = [f"{names.get(p, p)} = {v}" for p, v in zip(df["param"], df["value"])]
    df = df.iloc[::-1]
    fig, ax = plt.subplots(figsize=(10, 5.2), facecolor=SURFACE)
    style(ax, "Sharpe ratio when one parameter is changed (base config shown as line)")
    ax.grid(axis="y", visible=False)
    ax.grid(axis="x", color=GRID, linewidth=0.8)
    ax.axvline(base, color=INK2, linewidth=1.5, linestyle="--", zorder=1)
    ax.barh(df["label"], df["sharpe"], color=S1, height=0.6, zorder=2)
    ax.annotate(
        f"base {base:.2f}", (base, len(df) - 0.4), xytext=(4, 0), textcoords="offset points", fontsize=9, color=INK2
    )
    for y, v in enumerate(df["sharpe"]):
        ax.annotate(
            f"{v:.2f}",
            (v, y),
            xytext=(-4, 0),
            textcoords="offset points",
            va="center",
            ha="right",
            fontsize=8,
            color=SURFACE,
            fontweight="bold",
            zorder=3,
        )
    ax.set_xlabel("Annualised Sharpe ratio (net of fees)", color=INK2, fontsize=9)
    ax.set_xlim(0, max(df["sharpe"].max(), base) * 1.18)
    fig.text(
        0.01,
        0.005,
        "Every perturbation stays positive; no cliff. Source: reports/backtest/" + VERSION + "/perturbation.csv",
        fontsize=8,
        color=INK2,
    )
    fig.tight_layout(rect=(0, 0.02, 1, 1))
    fig.savefig(OUT / "robustness.png", dpi=160, facecolor=SURFACE)
    plt.close(fig)


def live_nav() -> None:
    f = ROOT / "reports" / "live" / "nav_hourly.csv"
    if not f.exists():
        return
    nav = pd.read_csv(f, index_col=0, parse_dates=True).iloc[:, 0]
    fig, (a1, a2) = plt.subplots(2, 1, figsize=(10, 5.6), sharex=True, height_ratios=[2.2, 1], facecolor=SURFACE)
    style(a1, "Live contest: Team105 portfolio value on Roostoo (hourly)")
    a1.plot(nav.index, nav / 1e3, color=S1, linewidth=2)
    a1.axhline(100, color=INK2, linewidth=1, linestyle="--")
    end_label(a1, nav.index[-1], nav.iloc[-1] / 1e3, f"{nav.iloc[-1] / 1e5 - 1:+.2%}", S1)
    a1.set_ylabel("Portfolio value ($k)", color=INK2, fontsize=9)
    dd = (nav / nav.cummax() - 1) * 100
    style(a2)
    a2.plot(dd.index, dd, color=S1, linewidth=2)
    a2.set_ylabel("Drawdown (%)", color=INK2, fontsize=9)
    a2.annotate(
        f"max {dd.min():.2f}%",
        (dd.idxmin(), dd.min()),
        xytext=(6, -2),
        textcoords="offset points",
        fontsize=9,
        color=INK,
        va="top",
    )
    fig.text(0.01, 0.005, "Source: bot NAV log (reports/live/nav_hourly.csv), start $100,000", fontsize=8, color=INK2)
    fig.tight_layout(rect=(0, 0.02, 1, 1))
    fig.savefig(OUT / "live_nav.png", dpi=160, facecolor=SURFACE)
    plt.close(fig)


if __name__ == "__main__":
    OUT.mkdir(parents=True, exist_ok=True)
    equity_curve()
    robustness()
    live_nav()
    print("charts written to", OUT)
