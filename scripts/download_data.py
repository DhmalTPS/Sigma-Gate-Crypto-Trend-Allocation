"""Download hourly history for every Roostoo pair that has a Binance USDT market.

usage: python scripts/download_data.py [--days 400] [--refresh]
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from t105.data.history import load_universe_history  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=400)
    ap.add_argument("--interval", default="1h")
    ap.add_argument("--refresh", action="store_true")
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    cached = ROOT / "data" / "cache" / "exchange_info.json"
    try:
        info = requests.get("https://mock-api.roostoo.com/v3/exchangeInfo", timeout=15).json()
    except requests.RequestException:
        info = json.loads(cached.read_text())
    pairs = sorted(p for p, v in info["TradePairs"].items() if v.get("CanTrade"))
    hist = load_universe_history(pairs, a.interval, a.days, ROOT / "data" / "cache", a.refresh)
    summary = {p: {"bars": len(df), "start": str(df.index[0]), "end": str(df.index[-1])}
               for p, df in hist.items()}
    (ROOT / "data" / "cache" / f"summary_{a.interval}.json").write_text(json.dumps(summary, indent=1))
    cached.write_text(json.dumps(info, indent=1))
    print(f"{len(hist)}/{len(pairs)} pairs have history")


if __name__ == "__main__":
    main()
