# Deployment Runbook (AWS, per the organizer's guide)

Source: organizer's "Hackathon Guide: How to Sign In AWS and Launch Your Bot" (Notion).
Goal: the bot runs 24/7 on the provided EC2 VM from Oct 4 to Oct 17 with the
**competition ("deployment") keys**, restarts itself after a crash or reboot, and never
needs a human to place a trade.

## Organizer rules for the AWS account (do not try to work around them)

| Rule | Value |
|---|---|
| Region | **ap-southeast-2 (Sydney) only** |
| Instance type | **t3.medium only**, launched from `Hackathon-Starter-Template` |
| Number of instances | **exactly one** (a second one triggers automatic termination) |
| Connection | **Session Manager only** (SSH / EC2 Instance Connect are blocked; no key pair) |
| Disk | 30 GB EBS max (the bot needs < 1 GB) |
| Other services | none (no IAM, S3, ...) — EC2 only |

## 0. Before you start

- [ ] Accept the AWS invite email (captain or teammate inbox) — **expires 7 days** after sending.
- [ ] Push this repo to a **public GitHub repo** (the VM clones it). Check `extra_info/` is not in it.
- [ ] Keep the key files on your laptop only (`extra_info/api_keys/`, git-ignored).

## 1. Sign in

1. Open the portal: **https://d-906625dad1.awsapps.com/start**
2. Username = the email the invite was sent to → enter the emailed verification code →
   set a password → set up MFA (any authenticator app).
3. On the AWS access portal: click the **AWS Account** tile → on the `Hackathon-TeamX` line
   click **Management Console** (`HackathonPermissionSet`).

## 2. Region + EC2

1. Top-right region selector → **Asia Pacific (Sydney) ap-southeast-2**. (Check it every time.)
2. Search bar → **EC2**.

## 3. Launch the (one) instance from the template

1. Left menu → **Instances → Launch Templates** → select **Hackathon-Starter-Template**.
2. **Actions → Launch instance from template**.
3. Key pair → **Proceed without a key pair**. Leave everything else default.
4. **Launch instance**. Optional: name/tag it `team105-bot`.

## 4. Connect (Session Manager)

1. **View all instances** → wait until *Status check* shows **2/2 (or 3/3) checks passed**.
2. Tick the instance → **Connect** → **Session Manager** tab → orange **Connect**.
   A black terminal opens in the browser. You have `sudo`.

## 5. Install the bot (copy-paste into the Session Manager terminal)

```bash
cd ~
sudo dnf install -y git
git clone https://github.com/<your-account>/<your-repo>.git t105
cd ~/t105
bash deployment/install.sh          # python 3.11 venv, deps, chrony clock sync, systemd unit
```

## 6. Put the competition keys on the VM (never in git)

There is no SSH/scp, so paste them in the editor:

```bash
sudo nano /etc/t105/deployment.env
```

Fill in the two lines with the contents of your local files
`extra_info/api_keys/deployment_api_key` and `deployment_api_secret`:

```
ROOSTOO_API_KEY=<paste>
ROOSTOO_API_SECRET=<paste>
```

Save: `Ctrl+O`, `Enter`, exit: `Ctrl+X`. Then `sudo chmod 600 /etc/t105/deployment.env`.

## 7. Pre-flight (no orders are sent)

```bash
cd ~/t105
.venv/bin/python -m pytest -q                         # expect: all passed
set -a; source <(sudo cat /etc/t105/deployment.env); set +a
.venv/bin/python scripts/run_bot.py --profile deployment --dry-run --once
unset ROOSTOO_API_KEY ROOSTOO_API_SECRET
tail -n 1 logs/deployment/$(date -u +%F)/health.jsonl
```

Check in that last line: `bar_sources` shows `binance` or `okx` for all 5 pairs, and the
balance read worked (no `book_unavailable` event). The dry run only *reads* the account.

## 8. Start it — recommended: systemd (survives crashes, reboots, closed browser tabs)

```bash
sudo systemctl enable --now t105-bot
sudo systemctl status t105-bot --no-pager       # should say: active (running)
journalctl -u t105-bot -f                       # live log; Ctrl+C stops watching only
```

It is safe to start **before** Oct 4: until `trade_not_before_utc` (Oct 3 16:00 UTC =
Oct 4 00:00 HKT) the deployment profile computes and logs targets but sends no orders.

**Alternative (organizer's tmux method)** — only if systemd is not usable:
```bash
sudo dnf install -y tmux && tmux
cd ~/t105 && set -a; source <(sudo cat /etc/t105/deployment.env); set +a
.venv/bin/python scripts/run_bot.py --profile deployment
# detach: Ctrl+B then D   (do NOT type exit)    reattach later: tmux attach
```
tmux does **not** restart the bot after a crash or VM reboot — systemd does.

## 9. Daily routine Oct 4 – 17 (5 minutes)

1. Connect via Session Manager → `cd ~/t105 && bash deployment/healthcheck.sh`
   → service RUNNING, recent NAV line, few API failures.
2. Compare real NAV with the shadow books in `decision.jsonl`.
3. Any change → `docs/CHANGE_CONTROL.md` → commit + push on laptop →
   on VM: `git pull && sudo systemctl restart t105-bot`.

## Do NOT

* **Stop or terminate the instance** during Oct 4 – 17 (the guide mentions stopping to save
  cost — that would stop trading and can cost active trading days).
* Launch a second instance (automatic termination).
* Place trades by hand with the deployment keys, or edit code directly on the VM.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `timestamp` errors | `sudo systemctl restart chronyd`; the bot re-syncs its offset every 10 min |
| `bar_sources` shows `local`/`none` | VM can't reach Binance/OKX → bot uses bundled history + Roostoo snapshots; check outbound HTTPS |
| `shorts unavailable` in log | competition disallows shorts → bot automatically trades long/flat |
| service keeps restarting | `journalctl -u t105-bot -n 100`; positions are re-read from the exchange on every start |
| Session Manager tab closed | nothing happens — the systemd service keeps running |
