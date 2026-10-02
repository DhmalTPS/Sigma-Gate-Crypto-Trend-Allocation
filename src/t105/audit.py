"""Append-only JSONL audit trail (one file per UTC day per stream).

Streams: api (every request), decision (every strategy cycle), order, fill,
nav, health. Together they let anyone reconstruct
data -> signal -> target -> order -> fill -> NAV, which is exactly what the
"Trade Log Integrity" screen asks us to demonstrate.
"""
from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timezone
from pathlib import Path


class Audit:
    def __init__(self, root: Path, run_id: str | None = None):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.run_id = run_id or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        self.lock = threading.Lock()

    def write(self, stream: str, rec: dict) -> None:
        now = datetime.now(timezone.utc)
        rec = {"ts": now.isoformat(timespec="milliseconds"), "epoch_ms": int(time.time() * 1000),
               "run": self.run_id, **rec}
        day = self.root / now.strftime("%Y-%m-%d")
        day.mkdir(exist_ok=True)
        line = json.dumps(rec, default=str, separators=(",", ":"))
        with self.lock, open(day / f"{stream}.jsonl", "a", encoding="utf-8") as f:
            f.write(line + "\n")

    def api_sink(self):
        return lambda rec: self.write("api", rec)
