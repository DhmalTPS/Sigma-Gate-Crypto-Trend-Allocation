"""Export live-contest evidence from the bot's own logs (READ-ONLY).

Prints an hourly NAV CSV and a daily summary between markers, so the output can be
copied from the VM terminal into reports/live/ in the repo (the VM has no git push).

usage (on the VM): cd ~/t105 && .venv/bin/python scripts/export_live.py
"""

from __future__ import annotations

import argparse
import glob
import json
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from t105 import config as CFG  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--profile", default="deployment")
    ap.add_argument("--since", default=None, help="UTC start (default: contest start from config)")
    a = ap.parse_args()
    cfg = CFG.load(ROOT)
    start = pd.Timestamp(a.since or cfg["live"].get("trade_not_before_utc", "1970-01-01"), tz="UTC")
    rows = []
    for f in sorted(glob.glob(str(ROOT / "logs" / a.profile / "*" / "nav.jsonl"))):
        for line in open(f, encoding="utf-8"):
            try:
                d = json.loads(line)
                rows.append((pd.Timestamp(d["ts"]), d["nav"]))
            except (json.JSONDecodeError, KeyError):
                pass
    if not rows:
        print(f"no NAV log found under logs/{a.profile}/")
        return
    nav = pd.Series(dict(rows)).sort_index()
    nav = nav[nav.index >= start]
    hourly = nav.resample("1h").last().dropna().round(2)
    if hourly.empty:
        print("no NAV snapshots after", start)
        return
    print("-----BEGIN nav_hourly.csv-----")
    print("ts,nav")
    for t, v in hourly.items():
        print(f"{t:%Y-%m-%dT%H:%M}Z,{v}")
    print("-----END nav_hourly.csv-----")
    print(f"rows={len(hourly)} first={hourly.index[0]} last={hourly.index[-1]}")


if __name__ == "__main__":
    main()
