"""Roostoo REST client.

Design rules (each one prevents a real production failure mode):
  * Signature = HMAC-SHA256(secret, "k1=v1&k2=v2..." sorted by key). The exact
    string we sign is the exact string we send, so formatting can never drift.
  * Timestamps use a server-clock offset re-synced periodically (server rejects
    |drift| > 60s; EC2 clocks are usually fine but we do not rely on it).
  * A client-side token bucket keeps us far below "excessive requests".
  * Read-only calls are retried. Order-creating calls are NEVER blindly retried
    on an ambiguous failure (timeout after send): the caller gets
    `ApiResult(ambiguous=True)` and must reconcile via query_order first.
    This is what prevents accidental double fills.
  * Roostoo answers failures with HTTP 200 + Success=false; we always check it.
  * Every request is written to the audit log (no secrets) -- the competition's
    trade-log-integrity screen wants evidence of autonomous API usage.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

import requests

from .credentials import Credentials

log = logging.getLogger(__name__)


@dataclass
class ApiResult:
    ok: bool
    data: dict = field(default_factory=dict)
    err: str = ""
    ambiguous: bool = False     # request may or may not have reached the exchange
    latency_ms: float = 0.0


class RateLimiter:
    """Token bucket: `rate` requests per 60s, burst = rate/4."""

    def __init__(self, per_minute: int):
        self.capacity = max(1.0, per_minute / 4)
        self.tokens = self.capacity
        self.fill_rate = per_minute / 60.0
        self.t = time.monotonic()
        self.lock = threading.Lock()

    def acquire(self) -> None:
        while True:
            with self.lock:
                now = time.monotonic()
                self.tokens = min(self.capacity, self.tokens + (now - self.t) * self.fill_rate)
                self.t = now
                if self.tokens >= 1:
                    self.tokens -= 1
                    return
                wait = (1 - self.tokens) / self.fill_rate
            time.sleep(wait)


def sign(secret: str, params: dict[str, Any]) -> tuple[str, str]:
    total = "&".join(f"{k}={params[k]}" for k in sorted(params))
    sig = hmac.new(secret.encode(), total.encode(), hashlib.sha256).hexdigest()
    return total, sig


class RoostooClient:
    def __init__(self, creds: Credentials | None, base_url: str = "https://mock-api.roostoo.com",
                 max_rpm: int = 30, timeout: float = 10.0,
                 audit: Callable[[dict], None] | None = None):
        self.creds = creds
        self.base = base_url.rstrip("/")
        self.timeout = timeout
        self.rl = RateLimiter(max_rpm)
        self.s = requests.Session()
        self.offset_ms = 0
        self._last_sync = 0.0
        self.audit = audit or (lambda rec: None)
        self.consecutive_failures = 0

    # ------------------------------------------------------------------ time
    def now_ms(self) -> int:
        if time.monotonic() - self._last_sync > 600:
            self.sync_time()
        return int(time.time() * 1000) + self.offset_ms

    def sync_time(self) -> int:
        self._last_sync = time.monotonic()       # set first: avoid recursion on failure
        t0 = time.time()
        r = self._request("GET", "/v3/serverTime", {}, signed=False, retries=2)
        if r.ok and "ServerTime" in r.data:
            local_mid = int((t0 + time.time()) / 2 * 1000)
            self.offset_ms = int(r.data["ServerTime"]) - local_mid
            if abs(self.offset_ms) > 5000:
                log.warning("local clock off by %d ms vs Roostoo; compensating", self.offset_ms)
        return self.offset_ms

    # ------------------------------------------------------------- transport
    def _request(self, method: str, path: str, params: dict, signed: bool,
                 retries: int = 3, idempotent: bool = True) -> ApiResult:
        attempt = 0
        while True:
            attempt += 1
            p = {k: v for k, v in params.items() if v is not None}
            if signed or "timestamp" in p:
                p["timestamp"] = str(self.now_ms()) if path != "/v3/serverTime" else p.get("timestamp")
            headers = {}
            total = "&".join(f"{k}={p[k]}" for k in sorted(p))
            if signed:
                if self.creds is None:
                    return ApiResult(False, err="no credentials for signed endpoint")
                total, sig = sign(self.creds.secret, p)
                headers = {"RST-API-KEY": self.creds.api_key, "MSG-SIGNATURE": sig}
            self.rl.acquire()
            t0 = time.perf_counter()
            sent = False
            try:
                if method == "GET":
                    resp = self.s.get(self.base + path, params=total or None, headers=headers,
                                      timeout=self.timeout)
                else:
                    headers["Content-Type"] = "application/x-www-form-urlencoded"
                    sent = True
                    resp = self.s.post(self.base + path, data=total, headers=headers,
                                       timeout=self.timeout)
                lat = (time.perf_counter() - t0) * 1000
                try:
                    body = resp.json()
                except ValueError:
                    body = {"raw": resp.text[:300]}
                ok = resp.status_code == 200 and bool(body.get("Success", "ServerTime" in body or "TradePairs" in body))
                err = "" if ok else str(body.get("ErrMsg") or f"HTTP {resp.status_code}")
                res = ApiResult(ok, body, err, False, lat)
            except requests.ConnectTimeout as e:      # never left the box: safe to retry
                res = ApiResult(False, err=f"connect-timeout: {e}", latency_ms=(time.perf_counter() - t0) * 1000)
            except requests.RequestException as e:
                res = ApiResult(False, err=f"{type(e).__name__}: {e}", ambiguous=sent and not idempotent,
                                latency_ms=(time.perf_counter() - t0) * 1000)
            self._audit(method, path, p, res, attempt)
            transport_err = not res.data and not res.ok
            self.consecutive_failures = 0 if res.ok else self.consecutive_failures + 1
            if res.ok or res.ambiguous or not transport_err or attempt > retries:
                return res
            if not idempotent and not res.err.startswith("connect-timeout"):
                return res
            time.sleep(min(8.0, 0.5 * 2 ** attempt))

    def _audit(self, method, path, params, res: ApiResult, attempt: int) -> None:
        rec = {"kind": "api", "method": method, "path": path,
               "params": {k: v for k, v in params.items() if k != "timestamp"},
               "ok": res.ok, "err": res.err, "ambiguous": res.ambiguous,
               "latency_ms": round(res.latency_ms, 1), "attempt": attempt}
        if path in ("/v3/place_order", "/v3/cancel_order", "/v6/short_open", "/v6/short_close"):
            rec["response"] = res.data
        self.audit(rec)

    # -------------------------------------------------------------- public
    def server_time(self) -> ApiResult:
        return self._request("GET", "/v3/serverTime", {}, signed=False)

    def exchange_info(self) -> ApiResult:
        return self._request("GET", "/v3/exchangeInfo", {}, signed=False)

    def ticker(self, pair: str | None = None) -> ApiResult:
        return self._request("GET", "/v3/ticker", {"pair": pair, "timestamp": ""}, signed=False)

    # -------------------------------------------------------------- signed reads
    def balance(self) -> ApiResult:
        return self._request("GET", "/v3/balance", {}, signed=True)

    def pending_count(self) -> ApiResult:
        r = self._request("GET", "/v3/pending_count", {}, signed=True)
        if not r.ok and "no pending order" in r.err:      # documented: Success=false when zero
            return ApiResult(True, {"TotalPending": 0, "OrderPairs": {}}, latency_ms=r.latency_ms)
        return r

    def query_order(self, order_id: int | str | None = None, pair: str | None = None,
                    pending_only: bool | None = None, limit: int | None = None,
                    offset: int | None = None) -> ApiResult:
        if order_id is not None:
            p = {"order_id": str(order_id)}       # docs: no other optional params allowed
        else:
            p = {"pair": pair,
                 "pending_only": None if pending_only is None else ("TRUE" if pending_only else "FALSE"),
                 "limit": None if limit is None else str(limit),
                 "offset": None if offset is None else str(offset)}
        r = self._request("POST", "/v3/query_order", p, signed=True)
        if not r.ok and "no order matched" in r.err:
            return ApiResult(True, {"OrderMatched": []}, latency_ms=r.latency_ms)
        return r

    def short_positions(self) -> ApiResult:
        return self._request("GET", "/v6/short_positions", {}, signed=True)

    # -------------------------------------------------------------- trading
    def place_order(self, pair: str, side: str, quantity: str, order_type: str,
                    price: str | None = None) -> ApiResult:
        p = {"pair": pair, "side": side, "type": order_type, "quantity": quantity}
        if order_type == "LIMIT":
            if price is None:
                return ApiResult(False, err="LIMIT requires price")
            p["price"] = price
        return self._request("POST", "/v3/place_order", p, signed=True, retries=1, idempotent=False)

    def cancel_order(self, order_id: int | str | None = None, pair: str | None = None) -> ApiResult:
        if order_id is not None and pair is not None:
            raise ValueError("send order_id OR pair, not both")
        p = {"order_id": None if order_id is None else str(order_id), "pair": pair}
        # cancel is naturally idempotent (cancelling twice is harmless)
        return self._request("POST", "/v3/cancel_order", p, signed=True, retries=2)

    def short_open(self, pair: str, collateral: str, price: str | None = None) -> ApiResult:
        p = {"pair": pair, "collateral": collateral}
        if price is not None:
            p["order_type"], p["price"] = "LIMIT", price
        return self._request("POST", "/v6/short_open", p, signed=True, retries=1, idempotent=False)

    def short_close(self, pair: str, close_qty: str | None = None,
                    close_pct: str | None = None) -> ApiResult:
        p = {"pair": pair, "close_qty": close_qty, "close_pct": None if close_qty else close_pct}
        return self._request("POST", "/v6/short_close", p, signed=True, retries=1, idempotent=False)
