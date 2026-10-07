"""Regression tests for the 2026-10-07 02:01 UTC incident: one coin's latest bar was stale."""

import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import t105.live.market as M  # noqa: E402
from t105 import config as CFG  # noqa: E402
from t105.live.runner import hold_unreliable  # noqa: E402
from t105.portfolio.policy import PolicyState, target_weights  # noqa: E402

PAIRS = ["BTC/USD", "ETH/USD", "SOL/USD", "BNB/USD", "XRP/USD"]


def _inputs():
    rng = np.random.default_rng(3)
    alpha = pd.Series([1.0, 1 / 3, 1.0, 1.0, -1 / 3], index=PAIRS)
    vol = pd.Series([0.0045, 0.0055, 0.0075, 0.005, 0.008], index=PAIRS)
    rets = pd.DataFrame(rng.normal(0, 0.006, (400, 5)), columns=PAIRS)
    regime = pd.Series({"mkt_trend": -0.2, "risk_on": 0.4})
    cur = pd.Series({"BTC/USD": 0.195, "ETH/USD": 0.171, "SOL/USD": 0.132, "BNB/USD": 0.191, "XRP/USD": -0.02})
    return alpha, vol, rets, regime, cur


def test_unreliable_coin_is_held_and_others_unchanged():
    pol = CFG.load(ROOT)["policy"]
    alpha, vol, rets, regime, cur = _inputs()
    w_full, _ = target_weights(alpha, vol, regime, rets, cur, 1e5, PolicyState(1e5), pol)
    w = hold_unreliable(w_full, cur, ["XRP/USD"])
    assert w["XRP/USD"] == cur["XRP/USD"]  # no trade on bad data (old bug: target 0 -> short closed)
    for p in ["BTC/USD", "ETH/USD", "SOL/USD", "BNB/USD"]:
        assert w[p] == w_full[p]  # risk budgets of the others NOT re-normalised


def test_old_behaviour_would_have_inflated_other_coins():
    """Documents the bug: dropping a coin's alpha re-normalises everyone else upward."""
    pol = CFG.load(ROOT)["policy"]
    alpha, vol, rets, regime, cur = _inputs()
    w_full, _ = target_weights(alpha, vol, regime, rets, cur * 0, 1e5, PolicyState(1e5), pol)
    a2 = alpha.copy()
    a2["XRP/USD"] = np.nan
    w_bug, _ = target_weights(a2, vol, regime, rets, cur * 0, 1e5, PolicyState(1e5), pol)
    assert w_bug["BNB/USD"] > w_full["BNB/USD"] * 1.1


def test_stale_fallback_data_does_not_block_snapshot_bar(monkeypatch):
    now_h = pd.Timestamp.now(tz="UTC").floor("h")
    old_idx = pd.date_range(now_h - pd.Timedelta(hours=50), periods=48, freq="1h", tz="UTC")
    old = pd.DataFrame(
        {"open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0, "volume": 1.0, "quote_volume": 1.0}, index=old_idx
    )
    # every remote source failed; the bundled snapshot returns OLD bars only
    monkeypatch.setattr(M, "fetch_with_fallback", lambda *a, **k: (old, "local"))
    mk = M.LiveMarket(client=None, pairs=["XRP/USD"])
    mk.bars["XRP/USD"] = old.copy()
    mk.snap[(now_h - pd.Timedelta(hours=1), "XRP/USD")] = [1.50, 1.48, 1.46]
    mk.update_bars()
    last = mk.bars["XRP/USD"].index[-1]
    assert last == now_h - pd.Timedelta(hours=1)  # the newest bar now exists
    assert mk.bars["XRP/USD"]["close"].iloc[-1] == 1.46  # built from Roostoo snapshots
    assert mk.last_update_src["XRP/USD"] == "roostoo_snapshot"
