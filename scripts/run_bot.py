"""Entry point for the autonomous bot.

usage:
  python scripts/run_bot.py --profile testing --dry-run     # compute + log, no orders
  python scripts/run_bot.py --profile testing               # trade the testing account
  python scripts/run_bot.py --profile deployment            # COMPETITION (run on EC2 via systemd)
"""
from __future__ import annotations

import argparse
import logging
import sys
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from t105.live.runner import Bot  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--profile", choices=["testing", "deployment"], required=True)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--config", default="config/strategy.yaml")
    ap.add_argument("--once", action="store_true", help="run a single cycle and exit")
    a = ap.parse_args()

    (ROOT / "logs").mkdir(exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    fh = TimedRotatingFileHandler(ROOT / "logs" / f"bot_{a.profile}.log", when="midnight", backupCount=30,
                                  encoding="utf-8")
    fh.setFormatter(fmt)
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    logging.basicConfig(level=logging.INFO, handlers=[fh, sh])

    bot = Bot(ROOT, a.profile, dry_run=a.dry_run, strategy_file=a.config)
    if a.once:
        bot.setup()
        bot.cycle()
        return
    bot.run()


if __name__ == "__main__":
    main()
