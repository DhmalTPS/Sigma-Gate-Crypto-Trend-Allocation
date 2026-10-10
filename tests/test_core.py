import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from t105 import metrics as M  # noqa: E402
from t105.api.client import sign  # noqa: E402
from t105.api.models import PairInfo  # noqa: E402
from t105.audit import Audit  # noqa: E402
from t105.portfolio.policy import PolicyParams, PolicyState, drawdown_multiplier, target_weights  # noqa: E402
from t105.risk.compliance import Compliance, RiskLimits  # noqa: E402
from t105.strategy.core import StrategyParams, compute  # noqa: E402


# ------------------------------------------------------------------ API
def test_signature_matches_roostoo_doc_example():
    params = {"pair": "BNB/USD", "quantity": "2000", "side": "BUY", "timestamp": "1580774512000", "type": "MARKET"}
    total, sig = sign("S1XP1e3UZj6A7H5fATj0jNhqPxxdSJYdInClVN65XAbvqqMKjVHjA7PZj4W12oep", params)
    assert total == "pair=BNB/USD&quantity=2000&side=BUY&timestamp=1580774512000&type=MARKET"
    assert sig == "20b7fd5550b67b3bf0c1684ed0f04885261db8fdabd38611e9e6af23c19b7fff"


def test_pairinfo_rounding_never_crosses():
    pi = PairInfo("BTC/USD", "BTC", 2, 5, 1.0, True)
    assert pi.fmt_qty(0.123456789) == "0.12345"  # floor, never round up
    assert pi.round_price(100.019, "BUY") == 100.01  # buys round down
    assert pi.round_price(100.011, "SELL") == 100.02  # sells round up
    assert not pi.valid(0.00001, 50.0)  # below MiniOrder notional


# ------------------------------------------------------------------ metrics
def test_metrics_basic():
    nav = pd.Series([100, 110, 99, 120.0])
    assert M.max_drawdown(nav.values) == pytest.approx(0.1)
    r = np.diff(nav.values) / nav.values[:-1]
    assert M.sortino(r) > 0 and M.sharpe(r) > 0
    assert 0 <= M.probabilistic_sharpe(np.random.default_rng(0).normal(0.001, 0.01, 500)) <= 1


# ------------------------------------------------------------------ policy invariants
def _row(n=10, seed=0):
    rng = np.random.default_rng(seed)
    cols = [f"C{i}/USD" for i in range(n)]
    alpha = pd.Series(rng.uniform(-1, 1, n), index=cols)
    vol = pd.Series(rng.uniform(0.005, 0.02, n), index=cols)
    rets = pd.DataFrame(rng.normal(0, 0.01, (400, n)), columns=cols)
    regime = pd.Series({"mkt_trend": -0.5, "risk_on": 0.5})
    return alpha, vol, rets, regime


@pytest.mark.parametrize("mode", ["long_only", "long_short", "neutral"])
def test_policy_respects_no_leverage_and_caps(mode):
    alpha, vol, rets, regime = _row()
    p = PolicyParams(mode=mode, max_weight=0.2)
    w, _ = target_weights(alpha * 3, vol, regime, rets, pd.Series(dtype=float), 1e5, PolicyState(1e5), p)
    assert w.abs().sum() <= 1.0 + 1e-9
    assert (w.abs() <= 0.35 + 1e-9).all()
    if mode == "long_only":
        assert (w >= 0).all()


def test_drawdown_governor_monotone():
    p = PolicyParams()
    m = [drawdown_multiplier(1 - dd, 1.0, p) for dd in (0, 0.02, 0.05, 0.08, 0.2)]
    assert m == sorted(m, reverse=True) and m[0] == 1.0 and m[-1] == p.dd_floor


def test_no_trade_band_holds_small_changes():
    alpha, vol, rets, regime = _row()
    p = PolicyParams(mode="long_only")
    st = PolicyState(1e5)
    w1, _ = target_weights(alpha, vol, regime, rets, pd.Series(dtype=float), 1e5, st, p)
    w2, _ = target_weights(alpha * 1.01, vol, regime, rets, w1, 1e5, st, p)
    assert np.allclose(w1.reindex(w2.index).fillna(0), w2)


# ------------------------------------------------------------------ no look-ahead
def test_strategy_is_causal():
    rng = np.random.default_rng(1)
    idx = pd.date_range("2025-01-01", periods=24 * 50, freq="1h", tz="UTC")
    cols = [f"C{i}/USD" for i in range(8)]
    close = pd.DataFrame(np.exp(np.cumsum(rng.normal(0, 0.01, (len(idx), 8)), axis=0)) * 10, idx, cols)
    qv = pd.DataFrame(1e8, idx, cols)
    p = StrategyParams(min_history_h=24 * 5)
    full = compute(close, qv, p)
    cut = 24 * 40
    trunc = compute(close.iloc[:cut], qv.iloc[:cut], p)
    t = idx[cut - 1]
    a, b = full.alpha.loc[t], trunc.alpha.loc[t]
    assert np.allclose(a.fillna(0), b.fillna(0), atol=1e-9), "alpha at t changed when future data was removed"


# ------------------------------------------------------------------ compliance
def test_compliance_blocks(tmp_path):
    c = Compliance(RiskLimits(), {"BTC/USD"}, Audit(tmp_path))
    assert not c.check("DOGE/USD", "BUY", 100, 1e5, 0.5, {}, True, 0)[0]  # not in universe
    assert not c.check("BTC/USD", "BUY", 50_000, 1e5, 0.5, {}, True, 0)[0]  # > 25% NAV
    assert not c.check("BTC/USD", "BUY", 100, 1e5, 1.2, {}, True, 0)[0]  # leverage
    assert not c.check("BTC/USD", "BUY", 100, 1e5, 0.5, {"BTC/USD": {"SELL"}}, True, 0)[0]  # MM-like
    assert c.check("BTC/USD", "SELL", 60_000, 1e5, 0.2, {}, True, 0, risk_reducing=True)[0]


def test_endgame_long_basket_and_floor():
    from dataclasses import replace as _r

    alpha, vol, rets, regime = _row(5)
    alpha[:] = -1 / 3  # gates all down: end-game ignores them
    p = _r(
        PolicyParams(sizing="absolute"),
        endgame_long=0.60,
        max_weight=0.30,
        max_weight_major=0.40,
        band_abs=0.0,
        band_rel=0.0,
        dd_soft=0.06,
        dd_hard=0.15,
    )
    w, d = target_weights(alpha, vol, regime, rets, pd.Series(dtype=float), 97_500, PolicyState(100_900), p)
    assert d["endgame"] and (w >= 0).all() and abs(w.sum() - 0.60) < 1e-6
    w2, d2 = target_weights(alpha, vol, regime, rets, w, 95_400, PolicyState(100_900), p)
    assert d2["endgame_floor_hit"] and w2.abs().sum() == 0  # contest return -4.6% -> flat
