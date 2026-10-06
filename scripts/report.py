"""Holistic READ-ONLY status report: service, performance, risk metrics, holdings,
signals (and how far each trend gate is from flipping), orders, fees, API health,
shadow books, compliance, contest activity days.

Never places, modifies or cancels orders. If ROOSTOO_API_KEY/SECRET are in the
environment it also makes read-only exchange calls (balance, order history,
short positions) for exchange-truth numbers; otherwise it uses local logs only.

usage (on the VM):
  cd ~/t105 && .venv/bin/python scripts/report.py [--profile deployment]
"""
from __future__ import annotations

import argparse
import glob
import json
import math
import os
import subprocess
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

W = 78


def hdr(t: str) -> None:
    print("\n" + "=" * W + f"\n {t}\n" + "=" * W)


def section(fn):
    def run(*a, **k):
        try:
            fn(*a, **k)
        except Exception as e:  # noqa: BLE001 -- one broken section must not hide the others
            print(f"  [section unavailable: {type(e).__name__}: {e}]")
    return run


def load_jsonl(logdir: Path, stream: str) -> list[dict]:
    out = []
    for f in sorted(glob.glob(str(logdir / "*" / f"{stream}.jsonl"))):
        with open(f, encoding="utf-8") as fh:
            for line in fh:
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    return out


def sh(cmd: str) -> str:
    try:
        return subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=10).stdout.strip()
    except Exception:  # noqa: BLE001
        return "?"


# ----------------------------------------------------------------------------- sections
@section
def s_service(cfg, profile):
    hdr("1. SERVICE & VERSION")
    print(f"  report time (UTC)   : {datetime.now(timezone.utc):%Y-%m-%d %H:%M:%S}")
    print(f"  systemd service     : {sh('systemctl is-active t105-bot') or 'n/a'}"
          f" | since {sh('systemctl show t105-bot -p ActiveEnterTimestamp --value') or 'n/a'}")
    print(f"  restarts (systemd)  : {sh('systemctl show t105-bot -p NRestarts --value') or 'n/a'}")
    print(f"  git commit          : {sh('git -C ' + str(ROOT) + ' log -1 --format=%h_%s')}")
    print(f"  strategy version    : {cfg['version']}   profile: {profile}")
    print(f"  trade_not_before    : {cfg['live'].get('trade_not_before_utc')}")
    print(f"  disk free           : {sh('df -h ' + str(ROOT) + ' | tail -1')}")


@section
def s_health(health):
    hdr("2. HEALTH EVENTS")
    ev = Counter(h.get("event") for h in health)
    print("  counts:", dict(ev))
    starts = [h for h in health if h.get("event") == "startup"]
    if starts:
        s = starts[-1]
        print(f"  last startup        : {s['ts'][:19]}  bars={s.get('bars_loaded')} "
              f"sources={s.get('bar_sources')} clock_offset_ms={s.get('clock_offset_ms')}")
    for h in [h for h in health if h.get("event") in ("exception", "circuit_breaker", "stale_data",
                                                       "book_unavailable")][-8:]:
        print(f"  ! {h['ts'][:19]} {h.get('event')}: {str(h.get('error') or h)[:110]}")


@section
def s_performance(navs, start_ts):
    hdr("3. PERFORMANCE & COMPETITION METRICS (from 5-min NAV snapshots)")
    from t105 import metrics as M
    df = pd.DataFrame([{"ts": pd.Timestamp(n["ts"]), "nav": n["nav"]} for n in navs]).set_index("ts")
    df = df[df.index >= start_ts]
    if len(df) < 3:
        print("  not enough NAV history yet")
        return
    nav = df["nav"]
    h = nav.resample("1h").last().dropna()
    r = h.pct_change().dropna().values
    init = 100_000.0
    peak = nav.cummax()
    dd = 1 - nav / peak
    print(f"  start / now         : {init:,.2f} -> {nav.iloc[-1]:,.2f}   return {nav.iloc[-1]/init-1:+.3%}")
    print(f"  high / low          : {nav.max():,.2f} ({nav.idxmax():%m-%d %H:%M}) / {nav.min():,.2f} ({nav.idxmin():%m-%d %H:%M})")
    print(f"  max drawdown        : {dd.max():.3%} (worst at {dd.idxmax():%m-%d %H:%M})   current dd {dd.iloc[-1]:.3%}")
    print(f"  hours of data       : {len(h)}   hourly vol (ann.) {np.std(r, ddof=1)*math.sqrt(8760):.2%}" if len(r) > 2 else "")
    if len(r) > 5:
        hv = np.concatenate([[init], h.values])
        print(f"  Sharpe (ann.)       : {M.sharpe(r):.2f}    Sortino (ann.) {M.sortino(r):.2f}")
        print(f"  Calmar ann./raw     : {M.calmar(hv):.2f} / {M.calmar(hv, annualise=False):.2f}")
        print(f"  composite 0.4So+0.3Sh+0.3Ca : {M.composite(hv):.2f}   (annualisation convention unknown: compare, don't over-read)")
        print(f"  P(true Sharpe>0)    : {M.probabilistic_sharpe(r):.2f}  (low until many days of data)")
    print("  daily (UTC):")
    print(f"    {'day':<11}{'open':>11}{'close':>11}{'ret':>8}{'maxDD':>8}{'low':>11}")
    for d, g in nav.groupby(nav.index.date):
        ddd = (1 - g / g.cummax()).max()
        print(f"    {str(d):<11}{g.iloc[0]:>11,.0f}{g.iloc[-1]:>11,.0f}{g.iloc[-1]/g.iloc[0]-1:>+8.2%}{ddd:>8.2%}{g.min():>11,.0f}")


@section
def s_position(decisions):
    hdr("4. CURRENT DECISION: HOLDINGS, TARGETS, RISK STATE")
    real = [d for d in decisions if not any(o.get("dry_run") for o in d.get("orders", []))]
    d = real[-1]
    dg = d.get("diag", {})
    print(f"  last cycle          : {d['ts'][:19]} UTC on bar {d['bar']}   cycles logged: {len(real)}")
    print(f"  NAV / peak(14d)     : {d['nav']:,.2f} / {d['peak']:,.2f}")
    print(f"  gross / net         : {dg.get('gross', 0):.3f} / {dg.get('net', 0):.3f}   positions: {dg.get('n')}")
    print(f"  basket vol / scale  : {dg.get('basket_vol', float('nan')):.2%} / {dg.get('vol_scale', float('nan')):.3f}"
          f"   (target vol 30%)")
    print(f"  drawdown multiplier : {dg.get('dd_mult')}  (1.0 = no de-risking; cuts start at 3% dd)")
    print(f"  shorts allowed now  : {dg.get('can_short')}   circuit breaker: {d.get('breaker')}")
    cur, tgt = d.get("current_w", {}), d.get("target_w", {})
    print(f"    {'pair':<10}{'current%':>10}{'target%':>10}{'signal':>9}")
    sig = {**d.get("alpha_bottom", {}), **d.get("alpha_top", {})}
    for p in sorted(set(cur) | set(tgt) | set(sig)):
        print(f"    {p:<10}{cur.get(p, 0)*100:>10.2f}{tgt.get(p, 0)*100:>10.2f}{sig.get(p, float('nan')):>9.3f}")
    reg = d.get("regime", {})
    print("  market regime       :", {k: round(v, 3) for k, v in reg.items() if v == v})


@section
def s_signals(cfg):
    hdr("5. TREND GATES PER COIN (live data) & DISTANCE TO FLIP")
    from t105.data.history import fetch_with_fallback
    sp = cfg["strategy"]
    end = int(time.time() * 1000)
    start = end - (max(sp.gate_lookbacks) + 24 * 4) * 3_600_000
    db = sp.gate_dead_band
    print(f"  gate flips to DOWN when z < -{db}, to UP when z > +{db}; z = log-return / (hourly vol * sqrt(lookback))")
    print(f"    {'pair':<10}{'price':>12}" + "".join(f"{'ret'+str(lb//24)+'d':>9}{'z':>7}" for lb in sp.gate_lookbacks)
          + f"{'flip7d@':>12}")
    for p in sp.universe:
        df, src = fetch_with_fallback(p, start, end, ROOT / "data" / "bootstrap")
        c = df["close"]
        lr = np.log(c).diff()
        vol = float(np.sqrt((lr ** 2).ewm(halflife=sp.vol_halflife, min_periods=48).mean()).iloc[-1])
        row = f"    {p:<10}{c.iloc[-1]:>12,.4f}"
        for lb in sp.gate_lookbacks:
            if len(c) > lb:
                ret = math.log(c.iloc[-1] / c.iloc[-1 - lb])
                row += f"{math.exp(ret)-1:>+9.2%}{ret/(vol*math.sqrt(lb)):>7.2f}"
            else:
                row += f"{'n/a':>9}{'':>7}"
        lb = sp.gate_lookbacks[0]
        flip = c.iloc[-1 - lb] * math.exp(-db * vol * math.sqrt(lb)) if len(c) > lb else float("nan")
        row += f"{flip:>12,.4f}"
        print(row + f"  [{src}]")
    print("  flip7d@ = price below which the 7-day gate would turn DOWN (if it is currently UP)")


@section
def s_orders(orders):
    hdr("6. ORDERS SENT BY THE BOT (local log)")
    real = [o for o in orders if o.get("kind") in ("spot", "short_open", "short_close")]
    if not real:
        print("  none")
        return
    by = Counter((o.get("kind"), (o.get("request") or {}).get("type") or o.get("kind"), o.get("ok")) for o in real)
    print("  by (kind, type, accepted):", dict(by))
    errs = Counter(o.get("err") for o in real if not o.get("ok"))
    if errs:
        print("  rejections:", dict(errs))
    print(f"    {'time UTC':<17}{'pair':<10}{'side':<12}{'type':<8}{'qty':>14}{'price':>12}{'status':>10}  reason")
    for o in real[-25:]:
        rq, rs = o.get("request") or {}, (o.get("response") or {})
        det = rs.get("OrderDetail") or rs
        print(f"    {o['ts'][:16]:<17}{o['pair']:<10}{rq.get('side', o['kind']):<12}{str(rq.get('type', '-')):<8}"
              f"{str(rq.get('qty') or rq.get('collateral') or rq.get('close_qty') or ''):>14}"
              f"{str(det.get('FilledAverPrice') or det.get('Price') or rq.get('price') or ''):>12}"
              f"{str(det.get('Status') or ('OK' if o.get('ok') else 'REJ')):>10}  {o.get('reason', '')}")


@section
def s_exchange(cfg):
    hdr("7. EXCHANGE TRUTH (read-only API: balance, full order history, fees, shorts)")
    if not (os.environ.get("ROOSTOO_API_KEY") and os.environ.get("ROOSTOO_API_SECRET")):
        print("  skipped (keys not in environment). Run with the keys loaded to include this section.")
        return
    from t105.api.client import RoostooClient
    from t105.api.credentials import Credentials
    from t105.audit import Audit
    c = RoostooClient(Credentials("report", os.environ["ROOSTOO_API_KEY"], os.environ["ROOSTOO_API_SECRET"]),
                      max_rpm=20, audit=Audit(ROOT / "logs" / "report").api_sink())
    c.sync_time()
    tk = c.ticker()
    mids = {p: (v["MaxBid"] + v["MinAsk"]) / 2 for p, v in (tk.data.get("Data") or {}).items()
            if v.get("MaxBid") and v.get("MinAsk")}
    b = c.balance()
    wallet = (b.data.get("SpotWallet") or b.data.get("Wallet") or {}) if b.ok else {}
    total = 0.0
    print(f"    {'asset':<7}{'free':>16}{'locked':>14}{'value USD':>14}")
    for a, v in sorted(wallet.items()):
        f, l = float(v.get("Free", 0) or 0), float(v.get("Lock", 0) or 0)
        if not (f or l):
            continue
        val = (f + l) if a == "USD" else (f + l) * mids.get(f"{a}/USD", 0)
        total += val
        print(f"    {a:<7}{f:>16,.6f}{l:>14,.6f}{val:>14,.2f}")
    sp = c.short_positions()
    for p in (sp.data.get("Positions") or []) if sp.ok else []:
        total += float(p.get("UnrealizedPNL", 0) or 0)
        print(f"    SHORT {p['Pair']} qty {p.get('ShortQty')} entry {p.get('EntryPrice')} upnl {p.get('UnrealizedPNL')}")
    print(f"  exchange-valued NAV : {total:,.2f}  ({total/100000-1:+.3%})")
    allo, off = [], 0
    while True:
        r = c.query_order(limit=100, offset=off or None)   # Roostoo rejects offset=0
        batch = r.data.get("OrderMatched", []) if r.ok else []
        allo += batch
        if len(batch) < 100 or off > 5000:
            break
        off += 100
    filled = [o for o in allo if o.get("Status") == "FILLED"]
    fee = sum(float(o.get("CommissionChargeValue", 0) or 0) *
              (1 if o.get("CommissionCoin") in (None, "USD") else mids.get(f"{o.get('CommissionCoin')}/USD", 0))
              for o in filled)
    notional = sum(float(o.get("FilledQuantity", 0) or 0) * float(o.get("FilledAverPrice", 0) or 0) for o in filled)
    roles = Counter(o.get("Role") for o in filled)
    print(f"  orders total        : {len(allo)}  by status {dict(Counter(o.get('Status') for o in allo))}")
    print(f"  filled notional     : ${notional:,.0f}  (turnover {notional/1e5:.2f}x NAV)   fees paid ≈ ${fee:,.2f}")
    print(f"  fills by role       : {dict(roles)}  maker share {roles.get('MAKER', 0)/max(1, len(filled)):.0%}")
    days = sorted({datetime.fromtimestamp(int(o.get('FinishTimestamp') or o.get('CreateTimestamp'))/1000, timezone.utc).date()
                   for o in filled})
    print(f"  ACTIVE TRADING DAYS : {len(days)}  -> {[str(d) for d in days]}  (rule: >= 8)")
    print("  last 10 fills:")
    for o in sorted(filled, key=lambda o: int(o.get("FinishTimestamp") or 0))[-10:]:
        t = datetime.fromtimestamp(int(o.get("FinishTimestamp") or o.get("CreateTimestamp")) / 1000, timezone.utc)
        print(f"    {t:%m-%d %H:%M} {o['Pair']:<9}{o['Side']:<12}{o.get('Type',''):<7}{o.get('Role',''):<6}"
              f"qty {o.get('FilledQuantity')} @ {o.get('FilledAverPrice')} fee {o.get('CommissionChargeValue')} {o.get('CommissionCoin')}")


@section
def s_api(api):
    hdr("8. API HEALTH")
    if not api:
        print("  no api log")
        return
    lat = [a.get("latency_ms", 0) for a in api if a.get("latency_ms")]
    fails = [a for a in api if not a.get("ok")]
    print(f"  calls {len(api)}   failures {len(fails)} ({len(fails)/len(api):.1%})   ambiguous {sum(bool(a.get('ambiguous')) for a in api)}")
    print(f"  latency ms p50/p95/max: {np.percentile(lat,50):.0f} / {np.percentile(lat,95):.0f} / {max(lat):.0f}")
    print("  by endpoint:", dict(Counter(a.get("path") for a in api).most_common()))
    print("  failure reasons:")
    for err, n in Counter(str(a.get("err"))[:70] for a in fails).most_common(8):
        print(f"    {n:>5} x {err}")
    per_min = Counter(a["ts"][:16] for a in api)
    print(f"  max requests in any minute: {max(per_min.values())}  (self-limit 30/min; no-HFT rule)")


@section
def s_shadows(decisions):
    hdr("9. SHADOW PORTFOLIOS (same signals, alternative settings; reset to 100,000 at each bot restart)")
    real = [d for d in decisions if d.get("shadows")]
    last = real[-1]
    first_nav = None
    for d in real:
        if d.get("run") == last.get("run"):
            first_nav = d["nav"]
            break
    real_ret = last["nav"] / first_nav - 1 if first_nav else float("nan")
    print(f"  real bot since this run started: {real_ret:+.3%}")
    for s in last["shadows"]:
        print(f"  {s['shadow']:<16} nav {s['nav']:>12,.2f}  ({s['nav']/1e5-1:+.3%})  gross {s.get('gross')}")


@section
def s_compliance(comp, decisions, cfg):
    hdr("10. COMPLIANCE & ACTIVITY")
    print(f"  orders blocked by compliance gate: {len(comp)}", dict(Counter(c.get('blocked') for c in comp)) if comp else "")
    per_day = defaultdict(int)
    for d in decisions:
        for o in d.get("orders", []):
            if not o.get("dry_run") and (o.get("status") or o.get("ok")):
                per_day[d["ts"][:10]] += 1
    print("  accepted orders per UTC day (local log):", dict(per_day))
    lc = cfg["live"]
    print(f"  activity guard: from {lc.get('activity_guard_hour_utc', 12)}:00 UTC, "
          f"target >= {lc.get('min_fills_per_day', 2)} fills per UTC day")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--profile", default="deployment")
    a = ap.parse_args()
    from t105 import config as CFG
    cfg = CFG.load(ROOT)
    logdir = ROOT / "logs" / a.profile
    start_ts = pd.Timestamp(cfg["live"].get("trade_not_before_utc", "1970-01-01"), tz="UTC")
    navs, decisions = load_jsonl(logdir, "nav"), load_jsonl(logdir, "decision")
    print(f"TEAM105 SIGMA GATE: FULL STATUS REPORT  ({a.profile})")
    s_service(cfg, a.profile)
    s_health(load_jsonl(logdir, "health"))
    s_performance(navs, start_ts)
    s_position(decisions)
    s_signals(cfg)
    s_orders(load_jsonl(logdir, "order"))
    s_exchange(cfg)
    s_api(load_jsonl(logdir, "api"))
    s_shadows(decisions)
    s_compliance(load_jsonl(logdir, "compliance"), decisions, cfg)
    print("\n(read-only report: nothing was traded, changed or cancelled)")


if __name__ == "__main__":
    main()
