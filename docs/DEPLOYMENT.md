# Deployment Runbook (novice-friendly)

Goal: the bot runs 24/7 on an AWS EC2 VM from Oct 4 to Oct 17 with the
**competition ("deployment") keys**, restarts itself if it crashes, and never needs
a human to place a trade.

## 0. Before you start (today)

- [ ] Accept the **AWS invite** email (captain or teammate inbox). It **expires 7 days** after it was sent.
- [ ] Create a **public GitHub repo** (e.g. `Team105-Threshold-Crushers`) and push this repository
      (`git remote add origin <url> && git push -u origin main`). Confirm `extra_info/` is **not** in it.
- [ ] Keep the four key files only on your laptop (`extra_info/api_keys/`, git-ignored).

## 1. Launch the VM (AWS console)

1. Sign in through the link in the AWS guide. Choose the region the organizer specifies
   (if none: one close to Hong Kong, e.g. `ap-east-1` or `ap-southeast-1`).
2. EC2 → **Launch instance**
   - Name: `t105-bot` · AMI: **Amazon Linux 2023** (or Ubuntu 22.04)
   - Type: `t3.small` is plenty (the bot uses < 300 MB RAM, < 5 % CPU)
   - Key pair: create one, download the `.pem` file
   - Network: allow **SSH (22) from My IP only**. No inbound ports are needed for the bot.
3. Launch, then copy the instance's **Public IPv4**.

## 2. Install the bot

```bash
# from your laptop
ssh -i t105.pem ec2-user@<PUBLIC_IP>        # ubuntu@... on Ubuntu

# on the VM
sudo dnf install -y git || sudo apt-get install -y git
git clone https://github.com/<you>/<repo>.git ~/t105
cd ~/t105
bash deployment/install.sh
```

## 3. Put the competition keys on the VM (never in git)

```bash
sudo nano /etc/t105/deployment.env
#   ROOSTOO_API_KEY=<contents of extra_info/api_keys/deployment_api_key>
#   ROOSTOO_API_SECRET=<contents of extra_info/api_keys/deployment_api_secret>
sudo chmod 600 /etc/t105/deployment.env
```

## 4. Pre-flight on the VM (safe: testing keys / dry run)

```bash
cd ~/t105
.venv/bin/python -m pytest -q                      # all tests must pass
# optional: copy the TESTING keys to ~/t105/extra_info/api_keys/ to run
.venv/bin/python scripts/smoke_test.py             # read-only connectivity (testing account)
# dry-run against the competition account (reads balance, computes targets, NO orders):
set -a; source <(sudo cat /etc/t105/deployment.env); set +a
.venv/bin/python scripts/run_bot.py --profile deployment --dry-run --once
unset ROOSTOO_API_KEY ROOSTOO_API_SECRET
```

Check `logs/deployment/<today>/health.jsonl`: `bar_sources` should show `binance` or `okx`
for all 5 pairs (not `local`/`none`).

## 5. Start it

```bash
sudo systemctl enable --now t105-bot
journalctl -u t105-bot -f          # live log; Ctrl+C stops watching, not the bot
bash deployment/healthcheck.sh     # one-screen status
```

It is safe to start **before** Oct 4: until `trade_not_before_utc` (Oct 3 16:00 UTC =
Oct 4 00:00 HKT) the deployment profile computes and logs targets but sends no orders.
If the organizer announces a different start time, change that one line, commit, pull, restart.

## 6. Daily routine during Oct 4 – 17 (5 minutes)

1. `bash deployment/healthcheck.sh` → service RUNNING, recent NAV line, API failures low.
2. Look at `decision.jsonl` shadows vs real NAV (evidence for any change).
3. Any change → follow `docs/CHANGE_CONTROL.md` → `git pull && sudo systemctl restart t105-bot`.
4. **Never** place trades by hand with the deployment keys.

## 7. Troubleshooting

| Symptom | Fix |
|---|---|
| `timestamp` errors | `sudo systemctl restart chronyd`; the bot re-syncs its offset every 10 min |
| `bar_sources: none` | VM can't reach Binance/OKX → it uses `data/bootstrap` + Roostoo snapshots; check outbound HTTPS |
| `shorts unavailable` in log | competition disallows shorts → bot automatically trades long/flat |
| service keeps restarting | `journalctl -u t105-bot -n 100` and fix; positions are re-read from the exchange on every start |
