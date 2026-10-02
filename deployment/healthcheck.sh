#!/usr/bin/env bash
# Quick operational health view. Safe: reads local logs only, never calls trading APIs.
set -uo pipefail
REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
DAY="$(date -u +%Y-%m-%d)"
L="$REPO_DIR/logs/deployment/$DAY"

systemctl is-active --quiet t105-bot && echo "service: RUNNING" || echo "service: NOT RUNNING"
[ -d "$L" ] || { echo "no logs for $DAY yet"; exit 0; }
echo "--- last NAV snapshot";      tail -n 1 "$L/nav.jsonl" 2>/dev/null
echo "--- last decision (bar, nav, gross, orders)"
tail -n 1 "$L/decision.jsonl" 2>/dev/null | python3 -c 'import json,sys
d=json.loads(sys.stdin.read()); print(d["bar"], d["nav"], round(d["diag"].get("gross",0),3), len(d["orders"]), "orders")'
echo "--- API calls today: $(wc -l < "$L/api.jsonl" 2>/dev/null || echo 0), failures: $(grep -c '"ok":false' "$L/api.jsonl" 2>/dev/null || echo 0)"
echo "--- health events";         tail -n 5 "$L/health.jsonl" 2>/dev/null
