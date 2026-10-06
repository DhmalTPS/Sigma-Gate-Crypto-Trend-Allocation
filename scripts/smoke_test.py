"""Connectivity smoke test against the TESTING account.

Read-only by default. `--order-cycle` additionally runs a tiny order lifecycle on
the testing account (post a far-away limit -> query -> cancel; then optionally
a ~$15 market buy+sell). It refuses to run on the deployment profile: the
competition account must only ever be touched by the autonomous bot.

usage: python scripts/smoke_test.py [--order-cycle] [--market]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from t105.api.client import RoostooClient  # noqa: E402
from t105.api.credentials import load_credentials  # noqa: E402
from t105.api.models import parse_exchange_info  # noqa: E402
from t105.audit import Audit  # noqa: E402


def show(name, r):
    print(f"[{'OK ' if r.ok else 'ERR'}] {name:<16} {r.latency_ms:7.1f}ms  {r.err}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--order-cycle", action="store_true")
    ap.add_argument("--market", action="store_true")
    ap.add_argument("--pair", default="BTC/USD")
    a = ap.parse_args()

    creds = load_credentials("testing", ROOT)  # hard-wired: never deployment
    audit = Audit(ROOT / "logs" / "smoke")
    c = RoostooClient(creds, audit=audit.api_sink())
    print("credentials:", creds.fingerprint())
    print("clock offset ms:", c.sync_time())

    r = c.exchange_info()
    show("exchangeInfo", r)
    info = parse_exchange_info(r.data)
    r = c.ticker(a.pair)
    show("ticker", r)
    tk = r.data.get("Data", {}).get(a.pair, {})
    print("   ", tk)
    r = c.balance()
    show("balance", r)
    wallet = (
        {
            k: v
            for k, v in r.data.get("Wallet", r.data.get("SpotWallet", {})).items()
            if v.get("Free", 0) or v.get("Lock", 0)
        }
        if r.ok
        else r.data
    )
    print("    non-zero wallet:", json.dumps(wallet)[:400])
    if r.ok and "Wallet" not in r.data:
        print("    balance keys:", list(r.data.keys()))
    r = c.pending_count()
    show("pending_count", r)
    print("   ", r.data)
    r = c.query_order(limit=5)
    show("query_order", r)
    print("    last orders:", len(r.data.get("OrderMatched", [])))
    r = c.short_positions()
    show("short_positions", r)
    print("   ", str(r.data)[:300])

    if not a.order_cycle:
        return
    pi = info[a.pair]
    bid = float(tk["MaxBid"])
    px = pi.round_price(bid * 0.80, "BUY")  # 20% below: will not fill
    qty = pi.floor_qty(max(15.0 / px, 10**-pi.amount_precision))
    print(f"\n-- limit lifecycle: BUY {pi.fmt_qty(qty)} {a.pair} @ {pi.fmt_price(px)}")
    r = c.place_order(a.pair, "BUY", pi.fmt_qty(qty), "LIMIT", pi.fmt_price(px))
    show("place LIMIT", r)
    print("   ", r.data.get("OrderDetail"))
    oid = r.data.get("OrderDetail", {}).get("OrderID")
    if oid is not None:
        r = c.query_order(order_id=oid)
        show("query by id", r)
        r = c.cancel_order(order_id=oid)
        show("cancel", r)
        print("   ", r.data)
        r = c.query_order(order_id=oid)
        show("query after", r)
        print("    status:", [o.get("Status") for o in r.data.get("OrderMatched", [])])
    if a.market:
        ask = float(tk["MinAsk"])
        qty = pi.floor_qty(15.0 / ask)
        print(f"\n-- market round trip: {pi.fmt_qty(qty)} {a.pair}")
        r = c.place_order(a.pair, "BUY", pi.fmt_qty(qty), "MARKET")
        show("MARKET BUY", r)
        print("   ", r.data.get("OrderDetail"))
        filled = float(r.data.get("OrderDetail", {}).get("FilledQuantity", 0))
        if filled:
            r = c.place_order(a.pair, "SELL", pi.fmt_qty(filled), "MARKET")
            show("MARKET SELL", r)
            print("   ", r.data.get("OrderDetail"))


if __name__ == "__main__":
    main()
