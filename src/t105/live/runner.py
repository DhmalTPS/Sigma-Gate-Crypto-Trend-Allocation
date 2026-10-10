"""Autonomous live loop.

Timeline of one hour:
  :00:00+90s  CYCLE  bars -> strategy -> policy -> risk -> orders (limit at touch)
  every 60s   ticker refresh (1 request for all pairs)
  every 5m    book + NAV snapshot, circuit-breaker check
  next :00    resting limits are cancelled; anything still needed is re-decided,
              and names that already waited one full hour cross as taker.

Restart-safe: state.json only stores *policy memory* (peak NAV, hysteresis,
maker attempts). Positions are always re-read from the exchange.
"""

from __future__ import annotations

import json
import logging
import os
import signal
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from .. import config as CFG
from ..api.client import RoostooClient
from ..api.credentials import load_credentials
from ..api.models import parse_exchange_info
from ..audit import Audit
from ..data.universe import crypto_pairs, price_precisions
from ..execution.broker import Book, Broker
from ..portfolio.policy import PolicyParams, PolicyState, target_weights
from ..risk.compliance import Compliance
from ..strategy.core import compute

log = logging.getLogger("t105.bot")


def hold_unreliable(w: pd.Series, cur_w: pd.Series, unreliable: list) -> pd.Series:
    """Keep coins with unreliable data at their current weight (no trade on bad data).
    The other coins' targets are untouched."""
    if not unreliable:
        return w
    w = w.copy()
    for p in unreliable:
        w[p] = float(cur_w.get(p, 0.0))
    return w


class ShadowBook:
    """Paper portfolio of an alternative policy, marked hourly with an estimated cost.
    Gives live, counterfactual evidence for change decisions during the contest."""

    def __init__(self, name: str, pol: PolicyParams, cost_bps: float = 8.0):
        self.name, self.pol, self.cost = name, pol, cost_bps / 1e4
        # NAV in dollars so min-trade-size filters behave as in the real book
        self.nav, self.w, self.st = 100_000.0, pd.Series(dtype=float), PolicyState(peak_nav=100_000.0)
        self.last_px: pd.Series | None = None

    def step(self, out, px: pd.Series, t) -> dict:
        if self.last_px is not None and len(self.w):
            r = (px / self.last_px - 1).reindex(self.w.index).fillna(0)
            self.nav *= 1 + float((self.w * r).sum())
        w_new, _ = target_weights(
            out.alpha.loc[t],
            out.vol.loc[t],
            out.regime.loc[t],
            out.returns.tail(self.pol.cov_window),
            self.w,
            self.nav,
            self.st,
            self.pol,
        )
        turn = float((w_new - self.w.reindex(w_new.index).fillna(0)).abs().sum())
        self.nav *= 1 - turn * self.cost
        self.w, self.last_px = w_new[w_new != 0], px
        return {"shadow": self.name, "nav": round(self.nav, 2), "gross": round(float(self.w.abs().sum()), 4)}


class Bot:
    def __init__(self, root: Path, profile: str, dry_run: bool = False, strategy_file: str = "config/strategy.yaml"):
        self.root, self.profile, self.dry = root, profile, dry_run
        self.cfg = CFG.load(root, strategy_file)
        self.live_cfg = self.cfg["live"]
        self.audit = Audit(root / "logs" / profile)
        comp = self.cfg["competition"]
        self.client = RoostooClient(
            load_credentials(profile, root),
            comp["api"]["base_url"],
            comp["api"]["max_requests_per_minute"],
            audit=self.audit.api_sink(),
        )
        self.state_path = root / "state" / f"{profile}_state.json"
        self.state = self._load_state()
        self.running = True

    # ------------------------------------------------------------ state
    def _load_state(self) -> dict:
        if self.state_path.exists():
            try:
                return json.loads(self.state_path.read_text())
            except json.JSONDecodeError:
                log.error("corrupt state file; starting fresh policy memory")
        return {"peak_nav": 0.0, "active": {}, "maker_attempts": {}, "breaker_until": 0.0, "last_cycle_hour": None}

    def _save_state(self) -> None:
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.state, indent=1, default=str))
        os.replace(tmp, self.state_path)  # atomic on POSIX and Windows

    # ------------------------------------------------------------ setup
    def setup(self) -> None:
        from .market import LiveMarket

        self.client.sync_time()
        info = self.client.exchange_info()
        if not info.ok:
            raise RuntimeError(f"exchangeInfo failed: {info.err}")
        self.pairs = parse_exchange_info(info.data)
        self.pp = price_precisions(info.data)
        whitelist = set(self.cfg["strategy"].universe)
        self.universe = [
            p for p in crypto_pairs(info.data) if self.pairs[p].can_trade and (not whitelist or p in whitelist)
        ]
        missing = whitelist - set(self.universe)
        if missing:
            log.warning("configured pairs not tradeable on this account: %s", sorted(missing))
        self.broker = Broker(self.client, self.pairs, self.audit)
        self.comp = Compliance(self.cfg["risk"], set(self.universe), self.audit)
        self.market = LiveMarket(
            self.client,
            self.universe,
            self.live_cfg.get("lookback_h", 24 * 60),
            local_dir=self.root / "data" / "bootstrap",
        )
        log.info("bootstrapping %d pairs of hourly history ...", len(self.universe))
        self.market.bootstrap()
        self.market.refresh_ticker()
        pol = self.cfg["policy"]
        from dataclasses import replace

        # Counterfactuals on the same signals: one per parameter we might be tempted to change.
        self.shadows = [
            ShadowBook("no_shorts", replace(pol, mode="long_only")),
            ShadowBook("vol_target_20", replace(pol, target_vol_ann=0.20)),
            ShadowBook("vol_target_40", replace(pol, target_vol_ann=0.40)),
        ]
        self.audit.write(
            "health",
            {
                "event": "startup",
                "profile": self.profile,
                "dry_run": self.dry,
                "version": self.cfg["version"],
                "universe": len(self.universe),
                "bars_loaded": len(self.market.bars),
                "bar_sources": self.market.sources,
                "clock_offset_ms": self.client.offset_ms,
            },
        )

    # ------------------------------------------------------------ helpers
    def _nav(self, bk: Book) -> float:
        return bk.nav(self.market.mids())

    def _filled_today(self) -> int:
        r = self.client.query_order(limit=100)
        if not r.ok:
            return -1
        day0 = int(datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0).timestamp() * 1000)
        return sum(
            1
            for o in r.data.get("OrderMatched", [])
            if o.get("Status") == "FILLED" and int(o.get("FinishTimestamp", 0) or 0) >= day0
        )

    # ------------------------------------------------------------ the hourly cycle
    def cycle(self) -> None:
        t0 = time.time()
        pol: PolicyParams = self.cfg["policy"]
        ex = self.cfg["execution"]
        L = self.cfg["risk"]

        # 1) resting limits from the previous cycle are cancelled and re-decided
        pend = self.broker.own_pending()
        for o in pend:
            if not self.dry:
                self.client.cancel_order(order_id=o["OrderID"])
            att = self.state["maker_attempts"]
            att[o["Pair"]] = att.get(o["Pair"], 0) + 1

        # 2) data
        self.market.update_bars()
        if not self.market.refresh_ticker():
            log.error("ticker unavailable: skip cycle")
            return
        age = self.market.data_age_s()
        if age > L.stale_data_s:
            self.audit.write("health", {"event": "stale_data", "age_s": age})
            return

        # 3) truth from the exchange
        bk = self.broker.book()
        if bk is None:
            self.audit.write("health", {"event": "book_unavailable"})
            return
        mids = self.market.mids()
        nav = self._nav(bk)
        self.state["peak_nav"] = max(self.state.get("peak_nav", 0.0), nav)

        # 4) strategy (identical code path to the backtest)
        P = self.market.panels()
        out = compute(P["close"], P["quote_volume"], self.cfg["strategy"], self.pp)
        t = out.alpha.index[-1]
        alpha = out.alpha.loc[t].copy()
        # Data-integrity check: a coin whose latest bar disagrees with Roostoo's live price is
        # HELD at its current weight this cycle. Its alpha is NOT removed: removing it would
        # re-normalise the other coins' risk budgets (bug seen live 2026-10-07 02:01 UTC).
        ok = self.market.integrity_mask(P["close"].loc[t])
        unreliable = [p for p in alpha.index if not bool(ok.get(p, False))]
        self._unreliable = unreliable

        # 5) policy
        cur_val = bk.signed_values(mids)
        cur_w = pd.Series(cur_val, dtype=float) / nav if nav > 0 else pd.Series(dtype=float)
        st = PolicyState(
            peak_nav=self.state["peak_nav"],
            active=self.state.get("active", {}),
            nav_hist=self.state.get("nav_hist", []),
            endgame_lock=self.state.get("endgame_lock", ""),
        )
        breaker = time.time() < self.state.get("breaker_until", 0)
        if 1 - nav / self.state["peak_nav"] >= L.breaker_dd and not breaker:
            self.state["breaker_until"] = time.time() + L.breaker_cooldown_h * 3600
            breaker = True
            self.audit.write("health", {"event": "circuit_breaker", "nav": nav, "peak": self.state["peak_nav"]})
        w, diag = target_weights(
            alpha,
            out.vol.loc[t],
            out.regime.loc[t],
            out.returns.tail(pol.cov_window),
            cur_w,
            nav,
            st,
            pol,
            shorts_enabled=self.broker.shorts_supported,
        )
        w = hold_unreliable(w, cur_w, unreliable)
        diag["integrity_hold"] = unreliable
        if breaker:
            w = w * 0.0
        self.state["active"] = st.active
        self.state["nav_hist"] = st.nav_hist
        if st.endgame_lock and not self.state.get("endgame_lock"):
            self.audit.write("health", {"event": "endgame_lock", "kind": st.endgame_lock, "nav": nav})
        self.state["endgame_lock"] = st.endgame_lock

        # 6) orders (deployment keys never trade before the contest window opens)
        not_before = pd.Timestamp(0, tz="UTC")
        if self.profile == "deployment":
            not_before = pd.Timestamp(self.live_cfg.get("trade_not_before_utc", "1970-01-01"), tz="UTC")
        if pd.Timestamp.now(tz="UTC") < not_before:
            self.audit.write("health", {"event": "pre_contest_hold", "until": str(not_before)})
            w = cur_w.reindex(w.index).fillna(0.0) if len(cur_w) else w * 0.0
        orders = self._execute(
            w,
            cur_w,
            bk,
            nav,
            mids,
            ex,
            urgent=breaker or diag.get("dd_mult", 1) < 0.6 or bool(diag.get("endgame_floor_hit")),
        )

        # 7) daily activity guard: the rules require >= 8 active days "with enough trades"
        if not breaker and pd.Timestamp.now(tz="UTC") >= not_before:
            g = self._activity_guard(alpha, out, t, cur_w, bk, nav, mids, ex, pol, st)
            if g:
                orders = orders + g

        # 8) shadows + logging
        px = P["close"].loc[t]
        shadow = [s.step(out, px, t) for s in self.shadows]
        top = alpha.dropna().sort_values()
        self.audit.write(
            "decision",
            {
                "bar": t,
                "nav": round(nav, 2),
                "peak": round(self.state["peak_nav"], 2),
                "diag": diag,
                "breaker": breaker,
                "sleeve_w": out.sleeve_weights.loc[t].round(3).to_dict(),
                "regime": out.regime.loc[t].round(4).to_dict(),
                "alpha_top": top.tail(5).round(3).to_dict(),
                "alpha_bottom": top.head(5).round(3).to_dict(),
                "target_w": {k: round(v, 4) for k, v in w[w != 0].items()},
                "current_w": {k: round(v, 4) for k, v in cur_w[cur_w.abs() > 1e-4].items()},
                "orders": orders,
                "shadows": shadow,
                "elapsed_s": round(time.time() - t0, 1),
                "binance_ok": self.market.binance_ok,
                "bar_update_src": dict(self.market.last_update_src),
                "version": self.cfg["version"],
            },
        )
        self.state["last_cycle_hour"] = str(pd.Timestamp.now(tz="UTC").floor("h"))
        self._save_state()

    def _activity_guard(self, alpha, out, t, cur_w, bk, nav, mids, ex, pol, st) -> list:
        """Guarantee >= min_fills_per_day fills per UTC day with a small, strategy-consistent trade.

        Uses the *unbanded* target (the no-trade band is what normally suppresses trades on quiet
        days), moves the most mis-weighted coin toward it by 0.5%-2% NAV, as a MARKET order so the
        fill is certain (cost ~$0.50). If every coin is exactly on target it trims the largest
        holding by 0.5% NAV (risk-reducing). Never runs before guard hour or if fills can't be counted.
        """
        import copy
        from dataclasses import replace as dc_replace

        hour = datetime.now(timezone.utc).hour
        if hour < self.live_cfg.get("activity_guard_hour_utc", 12):
            return []
        need = int(self.live_cfg.get("min_fills_per_day", 2))
        filled = self._filled_today()
        if filled < 0 or filled >= need:
            return []
        raw_pol = dc_replace(pol, band_abs=0.0, band_rel=0.0, min_trade_usd=0.0)
        w_raw, _ = target_weights(
            alpha,
            out.vol.loc[t],
            out.regime.loc[t],
            out.returns.tail(pol.cov_window),
            cur_w,
            nav,
            copy.deepcopy(st),
            raw_pol,
            shorts_enabled=self.broker.shorts_supported,
        )
        idx = cur_w.index.union(w_raw.index)
        gap = w_raw.reindex(idx).fillna(0) - cur_w.reindex(idx).fillna(0)
        bad = set(getattr(self, "_unreliable", []))
        gap = gap[[p for p in gap.index if p in self.pairs and p in mids and p not in bad]]
        if len(gap) == 0:
            return []
        j = gap.abs().idxmax()
        lo, hi = 0.005, 0.02
        if abs(gap[j]) > 1e-4:
            step = float(np.sign(gap[j])) * min(max(abs(gap[j]), lo), hi)
        else:
            held = cur_w[cur_w.abs() > lo]
            if len(held) == 0:
                # flat book (e.g. end-game lock): a tiny 0.5 % buy of the calmest coin; the next cycle's
                # target (0) closes it -> 2 fills for the day at ~0.001 % NAV cost
                j = "BTC/USD" if "BTC/USD" in gap.index else gap.index[0]
                step = lo
            else:
                j = held.abs().idxmax()
                step = -float(np.sign(held[j])) * lo
        wg = cur_w.copy()
        wg[j] = cur_w.get(j, 0.0) + step
        self.audit.write(
            "health",
            {"event": "activity_guard", "fills_today": filled, "need": need, "pair": j, "step_w": round(step, 4)},
        )
        return self._execute(wg, cur_w, bk, nav, mids, ex, urgent=True, only=[j], reason="activity_guard")

    def _execute(
        self,
        w: pd.Series,
        cur_w: pd.Series,
        bk: Book,
        nav: float,
        mids: dict,
        ex,
        urgent: bool,
        only: list | None = None,
        reason: str = "rebalance",
    ) -> list:
        names = sorted(set(w.index) | set(cur_w.index))
        if only is not None:
            names = only
        plan = []
        for p in names:
            if p not in self.pairs or p not in mids:
                continue
            tw, cw = float(w.get(p, 0.0)), float(cur_w.get(p, 0.0))
            if abs(tw - cw) * nav < 1e-9:
                continue
            plan.append((p, tw, cw))
        # risk-reducing legs first: they free cash/collateral for the rest
        plan.sort(key=lambda x: abs(x[1]) - abs(x[2]))
        usd_free = bk.usd_free
        gross = float(cur_w.abs().sum())
        sent, n = [], 0
        att = self.state["maker_attempts"]
        for p, tw, cw in plan:
            px = mids[p]
            top = self.market.top(p) or (px, px)
            coin = self.pairs[p].coin
            short_now = bk.shorts.get(p, {}).get("qty", 0.0)
            long_now = bk.long_qty(coin)
            legs = []
            if tw >= 0:
                if short_now > 0:
                    legs.append(("short_close", short_now))
                d_usd = tw * nav - long_now * px
                if d_usd > 0:
                    legs.append(("BUY", d_usd / px))
                elif d_usd < 0:
                    legs.append(("SELL", min(-d_usd / px, bk.coins.get(coin, (0, 0))[0])))
            else:
                if long_now > 0:
                    legs.append(("SELL", bk.coins.get(coin, (0, 0))[0]))
                cur_short_usd = short_now * px
                d = -tw * nav - cur_short_usd
                if d > 0:
                    legs.append(("short_open", d))
                elif d < 0:
                    legs.append(("short_close", -d / px))
            for kind, amt in legs:
                usd = amt if kind == "short_open" else amt * px
                if (
                    usd < self.cfg["policy"].min_trade_usd
                    and not (kind == "SELL" and tw == 0)
                    and not (kind == "short_close" and tw >= 0)
                ):
                    continue
                reducing = kind in ("SELL", "short_close")
                cap = self.cfg["risk"].max_order_frac * nav
                if not reducing and usd > cap:  # slice: the next cycle completes the rest
                    amt, usd = amt * cap / usd, cap
                gross_after = gross + (-usd if reducing else usd) / nav
                ok, why = self.comp.check(
                    p,
                    kind if kind in ("BUY", "SELL") else "SHORT",
                    usd,
                    nav,
                    gross_after,
                    {},
                    True,
                    n,
                    risk_reducing=reducing,
                )
                if not ok:
                    continue
                n += 1
                rec = {
                    "pair": p,
                    "kind": kind,
                    "usd": round(usd, 2),
                    "target_w": round(tw, 4),
                    "current_w": round(cw, 4),
                    "reason": reason,
                }
                if self.dry:
                    rec["dry_run"] = True
                    sent.append(rec)
                    continue
                if kind in ("BUY", "SELL"):
                    patient = att.get(p, 0) < ex.maker_patience
                    use_limit = ex.use_maker and patient and not urgent
                    if kind == "BUY":
                        qty = min(amt, max(0.0, usd_free * 0.995) / (top[1] * (1 + ex.taker_fee)))
                    else:
                        qty = amt
                    price = top[0] if kind == "BUY" else top[1]  # join the passive side
                    o = self.broker.spot(
                        p, kind, qty, "LIMIT" if use_limit else "MARKET", price if use_limit else None, reason
                    )
                    rec.update(
                        {
                            "type": "LIMIT" if use_limit else "MARKET",
                            "status": (o or {}).get("Status"),
                            "order_id": (o or {}).get("OrderID"),
                        }
                    )
                    if o and kind == "BUY":
                        usd_free -= qty * price * (1 + ex.taker_fee)
                    if o and not use_limit:
                        att[p] = 0
                elif kind == "short_open":
                    o = self.broker.short_open(p, min(amt, usd_free * 0.99 / (1 + ex.short_fee)), reason)
                    rec.update({"type": "SHORT_MARKET", "status": (o or {}).get("Status")})
                    if o:
                        usd_free -= amt * (1 + ex.short_fee)
                else:
                    full = tw >= 0 or amt >= short_now * 0.999
                    o = self.broker.short_close(p, None if full else amt, reason)
                    rec.update({"type": "SHORT_CLOSE", "ok": bool(o)})
                gross = gross_after
                sent.append(rec)
            if abs(tw - cw) * nav < self.cfg["policy"].min_trade_usd:
                att[p] = 0
        return sent

    # ------------------------------------------------------------ monitor
    def monitor(self) -> None:
        bk = self.broker.book()
        if bk is None:
            return
        nav = self._nav(bk)
        peak = max(self.state.get("peak_nav", 0.0), nav)
        self.state["peak_nav"] = peak
        self.audit.write(
            "nav",
            {
                "nav": round(nav, 2),
                "peak": round(peak, 2),
                "dd": round(1 - nav / peak, 5),
                "usd_free": round(bk.usd_free, 2),
                "usd_lock": round(bk.usd_lock, 2),
                "n_long": len(bk.coins),
                "n_short": len(bk.shorts),
                "api_fail_streak": self.client.consecutive_failures,
            },
        )
        L = self.cfg["risk"]
        if 1 - nav / peak >= L.breaker_dd and time.time() >= self.state.get("breaker_until", 0):
            log.warning("intra-hour circuit breaker at NAV %.0f (peak %.0f)", nav, peak)
            self.state["breaker_until"] = time.time() + L.breaker_cooldown_h * 3600
            self._save_state()
            self.cycle()

    # ------------------------------------------------------------ main loop
    def run(self) -> None:
        signal.signal(signal.SIGTERM, lambda *_: setattr(self, "running", False))
        self.setup()
        offset = self.live_cfg.get("cycle_offset_s", 90)
        last_tick = last_mon = 0.0
        if self.live_cfg.get("cycle_on_start", True):
            self._safe(self.cycle)
        while self.running:
            now = time.time()
            hour = str(pd.Timestamp.now(tz="UTC").floor("h"))
            if hour != self.state.get("last_cycle_hour") and (now % 3600) >= offset:
                self._safe(self.cycle)
            if now - last_tick >= 60:
                self._safe(self.market.refresh_ticker)
                last_tick = now
            if now - last_mon >= 300:
                self._safe(self.monitor)
                last_mon = now
            time.sleep(5)
        self.audit.write("health", {"event": "shutdown"})

    def _safe(self, fn) -> None:
        try:
            fn()
        except Exception as e:  # noqa: BLE001 -- the loop must survive any single failure
            log.exception("error in %s", getattr(fn, "__name__", fn))
            self.audit.write(
                "health", {"event": "exception", "where": getattr(fn, "__name__", "?"), "error": repr(e)[:500]}
            )
            # make sure a failing cycle does not hot-loop: mark the hour as attempted
            self.state["last_cycle_hour"] = str(pd.Timestamp.now(tz="UTC").floor("h"))
