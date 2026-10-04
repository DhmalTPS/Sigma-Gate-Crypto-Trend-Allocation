"""Exchange-state reconciliation + order placement on Roostoo.

The exchange is the single source of truth. Every cycle starts by rebuilding
holdings from /v3/balance and /v6/short_positions -- never from local memory --
so a crash/restart can never cause the bot to "forget" a position and re-buy it.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

from ..api.client import ApiResult, RoostooClient
from ..api.models import PairInfo

log = logging.getLogger(__name__)


@dataclass
class Book:
    """Snapshot of what we actually own, from the exchange."""
    usd_free: float = 0.0
    usd_lock: float = 0.0
    coins: dict = field(default_factory=dict)      # coin -> (free, lock)
    shorts: dict = field(default_factory=dict)     # pair -> dict(qty, entry, collateral, upnl)
    ts: float = 0.0

    def long_qty(self, coin: str) -> float:
        f, l = self.coins.get(coin, (0.0, 0.0))
        return f + l

    def nav(self, mids: dict[str, float]) -> float:
        v = self.usd_free + self.usd_lock
        for coin, (f, l) in self.coins.items():
            px = mids.get(f"{coin}/USD")
            if px:
                v += (f + l) * px
        for s in self.shorts.values():
            v += s["upnl"]          # collateral already inside usd_lock
        return v

    def signed_values(self, mids: dict[str, float]) -> dict[str, float]:
        """pair -> signed USD exposure (long positive, short negative)."""
        out = {}
        for coin, (f, l) in self.coins.items():
            pair = f"{coin}/USD"
            if pair in mids and (f + l) > 0:
                out[pair] = (f + l) * mids[pair]
        for pair, s in self.shorts.items():
            if pair in mids:
                out[pair] = out.get(pair, 0.0) - s["qty"] * mids[pair]
        return out


class Broker:
    def __init__(self, client: RoostooClient, pairs: dict[str, PairInfo], audit):
        self.c = client
        self.pairs = pairs
        self.audit = audit
        self.shorts_supported = True
        self.shorts_retry_at = 0.0

    SHORTS_RECHECK_S = 6 * 3600

    def _disable_shorts(self, why: str) -> None:
        """Only an explicit 'competition does not allow short positions' disables shorts,
        and even then we re-check periodically (rules/permissions can change)."""
        log.warning("shorts disabled for %dh: %s", self.SHORTS_RECHECK_S // 3600, why)
        self.shorts_supported = False
        self.shorts_retry_at = time.time() + self.SHORTS_RECHECK_S

    # ------------------------------------------------------------ state
    def book(self) -> Book | None:
        if not self.shorts_supported and time.time() >= self.shorts_retry_at:
            self.shorts_supported = True
        b = self.c.balance()
        if not b.ok:
            log.error("balance failed: %s", b.err)
            return None
        wallet = b.data.get("SpotWallet") or b.data.get("Wallet") or {}
        bk = Book(ts=time.time())
        for coin, v in wallet.items():
            f, l = float(v.get("Free", 0) or 0), float(v.get("Lock", 0) or 0)
            if coin == "USD":
                bk.usd_free, bk.usd_lock = f, l
            elif f or l:
                bk.coins[coin] = (f, l)
        if self.shorts_supported:
            s = self.c.short_positions()
            if s.ok:
                for p in s.data.get("Positions", []) or []:
                    bk.shorts[p["Pair"]] = {"qty": float(p.get("ShortQty", 0) or 0),
                                            "entry": float(p.get("EntryPrice", 0) or 0),
                                            "collateral": float(p.get("Collateral", 0) or 0),
                                            "upnl": float(p.get("UnrealizedPNL", 0) or 0)}
            elif "does not allow" in s.err:
                self._disable_shorts(s.err)
            elif "permission" in s.err:
                # account-wide "no permission to trade" (e.g. before the contest opens):
                # no position can exist, and it must NOT switch shorts off for later.
                log.warning("short_positions: %s (treating as no open shorts)", s.err)
            else:
                log.error("short_positions failed: %s -- treating book as unknown", s.err)
                return None
        return bk

    def own_pending(self) -> list[dict]:
        r = self.c.query_order(pending_only=True, limit=100)
        return r.data.get("OrderMatched", []) if r.ok else []

    def cancel_all_pending(self) -> int:
        """Cancel resting limits from the previous cycle (they are re-decided each hour)."""
        pend = self.own_pending()
        n = 0
        for o in pend:
            r = self.c.cancel_order(order_id=o["OrderID"])
            n += int(r.ok)
        return n

    # ------------------------------------------------------------ orders
    def _record(self, kind: str, pair: str, req: dict, r: ApiResult, why: str) -> None:
        self.audit.write("order", {"kind": kind, "pair": pair, "request": req, "ok": r.ok,
                                   "err": r.err, "ambiguous": r.ambiguous, "reason": why,
                                   "response": r.data})

    def _resolve_ambiguous(self, pair: str, side: str, qty: str, t_send_ms: int) -> dict | None:
        """After a lost response: did the order reach the exchange? Look it up, never re-send."""
        time.sleep(2.0)
        r = self.c.query_order(pair=pair, limit=10)
        for o in r.data.get("OrderMatched", []) if r.ok else []:
            if o.get("Side") == side and abs(float(o.get("Quantity", 0)) - float(qty)) < 1e-12 \
                    and int(o.get("CreateTimestamp", 0)) >= t_send_ms - 5000:
                return o
        return None

    def spot(self, pair: str, side: str, qty: float, order_type: str, price: float | None,
             why: str) -> dict | None:
        pi = self.pairs[pair]
        q = pi.fmt_qty(qty)
        if float(q) <= 0:
            return None
        px = None if order_type == "MARKET" else pi.fmt_price(pi.round_price(price, side))
        ref = float(px) if px else (price or 0)
        if ref and float(q) * ref <= pi.min_notional * 1.05:
            return None
        t_send = self.c.now_ms()
        r = self.c.place_order(pair, side, q, order_type, px)
        self._record("spot", pair, {"side": side, "qty": q, "type": order_type, "price": px}, r, why)
        if r.ambiguous:
            o = self._resolve_ambiguous(pair, side, q, t_send)
            self.audit.write("order", {"kind": "ambiguous_resolution", "pair": pair, "found": o})
            return o
        return r.data.get("OrderDetail") if r.ok else None

    def short_open(self, pair: str, usd: float, why: str) -> dict | None:
        if not self.shorts_supported:
            return None
        coll = f"{max(0.0, usd):.2f}"
        if float(coll) < 5:
            return None
        r = self.c.short_open(pair, coll)
        self._record("short_open", pair, {"collateral": coll}, r, why)
        if not r.ok and "does not allow" in r.err:
            self._disable_shorts(r.err)
        return r.data if r.ok else None

    def short_close(self, pair: str, qty: float | None, why: str) -> dict | None:
        pi = self.pairs[pair]
        q = None if qty is None else pi.fmt_qty(qty)
        if q is not None and float(q) <= 0:
            return None
        r = self.c.short_close(pair, close_qty=q)
        self._record("short_close", pair, {"close_qty": q}, r, why)
        return r.data if r.ok else None
