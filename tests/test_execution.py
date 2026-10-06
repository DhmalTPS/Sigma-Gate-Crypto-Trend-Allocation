"""Execution-planner tests with a stub broker (no network)."""
import sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from t105 import config as CFG  # noqa: E402
from t105.api.models import PairInfo  # noqa: E402
from t105.audit import Audit  # noqa: E402
from t105.execution.broker import Book  # noqa: E402
from t105.live.runner import Bot  # noqa: E402
from t105.risk.compliance import Compliance  # noqa: E402


def test_book_nav_includes_shorts_and_locks():
    bk = Book(usd_free=1000, usd_lock=500, coins={"BTC": (0.01, 0.0)},
              shorts={"ETH/USD": {"qty": 0.1, "entry": 3000, "collateral": 300, "upnl": 20}})
    mids = {"BTC/USD": 50_000, "ETH/USD": 2800}
    assert bk.nav(mids) == 1000 + 500 + 500 + 20
    sv = bk.signed_values(mids)
    assert sv["BTC/USD"] == 500 and sv["ETH/USD"] == -280


class StubBroker:
    def __init__(self):
        self.calls = []
        self.shorts_supported = True

    def spot(self, pair, side, qty, typ, price, why):
        self.calls.append(("spot", pair, side, round(qty, 6), typ))
        return {"Status": "FILLED" if typ == "MARKET" else "PENDING", "OrderID": len(self.calls)}

    def short_open(self, pair, usd, why):
        self.calls.append(("short_open", pair, round(usd, 2)))
        return {"Status": "OPEN"}

    def short_close(self, pair, qty, why):
        self.calls.append(("short_close", pair, qty))
        return {"Success": True}


def _bot(tmp_path):
    bot = Bot.__new__(Bot)
    bot.cfg = CFG.load(ROOT)
    bot.dry = False
    bot.audit = Audit(tmp_path)
    bot.state = {"maker_attempts": {}, "peak_nav": 1e5, "active": {}}
    bot.pairs = {p: PairInfo(p, p.split("/")[0], 2, 5, 1.0, True) for p in ("BTC/USD", "ETH/USD")}
    bot.broker = StubBroker()
    bot.comp = Compliance(bot.cfg["risk"], set(bot.pairs), bot.audit)
    bot.market = SimpleNamespace(top=lambda p: (99.0, 101.0))
    return bot


def test_flip_long_to_short_sells_then_opens_short(tmp_path):
    bot = _bot(tmp_path)
    bk = Book(usd_free=90_000, coins={"BTC": (100.0, 0.0)})          # 100 BTC @100 = 10% long
    mids = {"BTC/USD": 100.0, "ETH/USD": 100.0}
    w = pd.Series({"BTC/USD": -0.05})
    cur = pd.Series({"BTC/USD": 0.10})
    sent = bot._execute(w, cur, bk, 1e5, mids, bot.cfg["execution"], urgent=False)
    kinds = [c[0] for c in bot.broker.calls]
    assert kinds == ["spot", "short_open"]
    assert bot.broker.calls[0][2] == "SELL" and bot.broker.calls[0][3] == 100.0
    assert abs(bot.broker.calls[1][2] - 5000) < 1
    assert len(sent) == 2


def test_cover_short_then_buy_long(tmp_path):
    bot = _bot(tmp_path)
    bk = Book(usd_free=95_000, shorts={"ETH/USD": {"qty": 50.0, "entry": 100, "collateral": 5000, "upnl": 0}})
    mids = {"BTC/USD": 100.0, "ETH/USD": 100.0}
    sent = bot._execute(pd.Series({"ETH/USD": 0.05}), pd.Series({"ETH/USD": -0.05}), bk, 1e5, mids,
                        bot.cfg["execution"], urgent=False)
    assert bot.broker.calls[0] == ("short_close", "ETH/USD", None)      # full close
    assert bot.broker.calls[1][:3] == ("spot", "ETH/USD", "BUY")
    assert bot.broker.calls[1][4] == "LIMIT"                             # patient -> maker first
    assert len(sent) == 2


def test_urgent_uses_market_and_patience_exhausted_uses_market(tmp_path):
    bot = _bot(tmp_path)
    bk = Book(usd_free=100_000)
    mids = {"BTC/USD": 100.0, "ETH/USD": 100.0}
    bot._execute(pd.Series({"BTC/USD": 0.1}), pd.Series(dtype=float), bk, 1e5, mids, bot.cfg["execution"], urgent=True)
    assert bot.broker.calls[-1][4] == "MARKET"
    bot.state["maker_attempts"]["ETH/USD"] = 1
    bot._execute(pd.Series({"ETH/USD": 0.1}), pd.Series(dtype=float), bk, 1e5, mids, bot.cfg["execution"], urgent=False)
    assert bot.broker.calls[-1][4] == "MARKET"


def test_oversized_buy_is_sliced_to_order_cap(tmp_path):
    bot = _bot(tmp_path)
    bk = Book(usd_free=100_000)
    mids = {"BTC/USD": 100.0, "ETH/USD": 100.0}
    bot._execute(pd.Series({"BTC/USD": 0.4}), pd.Series(dtype=float), bk, 1e5, mids, bot.cfg["execution"], urgent=False)
    assert len(bot.broker.calls) == 1
    assert abs(bot.broker.calls[0][3] * 100.0 - 25_000) < 1     # capped at 25% NAV this cycle


class _Res:
    def __init__(self, ok, data=None, err=""):
        self.ok, self.data, self.err = ok, data or {}, err


class _Client:
    def __init__(self, short_err):
        self.short_err = short_err

    def balance(self):
        return _Res(True, {"SpotWallet": {"USD": {"Free": 100000, "Lock": 0}}})

    def short_positions(self):
        return _Res(False, err=self.short_err)


def test_no_permission_does_not_disable_shorts(tmp_path):
    from t105.execution.broker import Broker
    b = Broker(_Client("your do not have permission to trade"), {}, Audit(tmp_path))
    bk = b.book()
    assert bk is not None and bk.usd_free == 100000
    assert b.shorts_supported            # pre-contest "no permission" must not kill shorts


def test_does_not_allow_disables_then_rechecks(tmp_path):
    from t105.execution.broker import Broker
    b = Broker(_Client("this competition does not allow short positions"), {}, Audit(tmp_path))
    assert b.book() is not None and not b.shorts_supported
    b.shorts_retry_at = 0.0               # recheck window elapsed
    b.c.short_err = "boom"
    b.book()
    assert b.shorts_supported or b.shorts_retry_at > 0


def _guard_bot(tmp_path, filled_today):
    import numpy as np
    from t105.portfolio.policy import PolicyState
    bot = _bot(tmp_path)
    bot.live_cfg = {"activity_guard_hour_utc": 0, "min_fills_per_day": 2}
    bot._filled_today = lambda: filled_today
    idx = pd.date_range("2026-10-01", periods=400, freq="1h", tz="UTC")
    cols = ["BTC/USD", "ETH/USD"]
    rng = np.random.default_rng(0)
    out = SimpleNamespace(vol=pd.DataFrame(0.01, idx, cols),
                          regime=pd.DataFrame({"mkt_trend": 0.2, "risk_on": 0.6}, index=idx),
                          returns=pd.DataFrame(rng.normal(0, 0.01, (400, 2)), idx, cols))
    alpha = pd.Series({"BTC/USD": 1.0, "ETH/USD": 1.0})
    return bot, out, idx[-1], alpha, PolicyState(peak_nav=1e5)


def test_activity_guard_trades_when_on_target_and_no_fills(tmp_path):
    from t105.portfolio.policy import PolicyState, target_weights
    bot, out, t, alpha, st = _guard_bot(tmp_path, filled_today=0)
    pol = bot.cfg["policy"]
    # current == banded target (the situation where the old guard did nothing)
    w, _ = target_weights(alpha, out.vol.loc[t], out.regime.loc[t], out.returns, pd.Series(dtype=float),
                          1e5, PolicyState(peak_nav=1e5), pol)
    cur = w[w != 0]
    bk = Book(usd_free=50_000, coins={"BTC": (cur.get("BTC/USD", 0) * 1e5 / 100, 0),
                                      "ETH": (cur.get("ETH/USD", 0) * 1e5 / 100, 0)})
    mids = {"BTC/USD": 100.0, "ETH/USD": 100.0}
    sent = bot._activity_guard(alpha, out, t, cur, bk, 1e5, mids, bot.cfg["execution"], pol, st)
    assert len(sent) == 1 and sent[0]["reason"] == "activity_guard"
    assert bot.broker.calls[-1][4] == "MARKET"                       # fill must be certain
    assert 400 <= sent[0]["usd"] <= 2100                             # 0.5%..2% NAV


def test_activity_guard_quiet_when_enough_fills_or_unknown(tmp_path):
    bot, out, t, alpha, st = _guard_bot(tmp_path, filled_today=2)
    mids = {"BTC/USD": 100.0, "ETH/USD": 100.0}
    cur = pd.Series({"BTC/USD": 0.2})
    assert bot._activity_guard(alpha, out, t, cur, Book(usd_free=80_000, coins={"BTC": (200.0, 0)}), 1e5, mids,
                               bot.cfg["execution"], bot.cfg["policy"], st) == []
    bot._filled_today = lambda: -1                                    # API failure: never trade blind
    assert bot._activity_guard(alpha, out, t, cur, Book(usd_free=80_000, coins={"BTC": (200.0, 0)}), 1e5, mids,
                               bot.cfg["execution"], bot.cfg["policy"], st) == []
