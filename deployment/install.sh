#!/usr/bin/env bash
# One-shot EC2 setup (Amazon Linux 2023 or Ubuntu 22.04+). Run as the default user:
#   git clone <repo> ~/t105 && cd ~/t105 && bash deployment/install.sh
# Then put the COMPETITION keys in /etc/t105/deployment.env (see .env.example) and:
#   sudo systemctl enable --now t105-bot
set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
USER_NAME="$(whoami)"

if command -v dnf >/dev/null 2>&1; then
  sudo dnf install -y python3.11 python3.11-pip git chrony
  PY=python3.11
else
  sudo apt-get update -y && sudo apt-get install -y python3 python3-venv python3-pip git chrony
  PY=python3
fi
# Accurate clock: Roostoo rejects signed requests with >60s drift.
sudo systemctl enable --now chronyd 2>/dev/null || sudo systemctl enable --now chrony

$PY -m venv "$REPO_DIR/.venv"
"$REPO_DIR/.venv/bin/pip" install --upgrade pip
"$REPO_DIR/.venv/bin/pip" install -r "$REPO_DIR/requirements.txt"

sudo mkdir -p /etc/t105
if [ ! -f /etc/t105/deployment.env ]; then
  sudo cp "$REPO_DIR/deployment/.env.example" /etc/t105/deployment.env
fi
sudo chown root:root /etc/t105/deployment.env
sudo chmod 600 /etc/t105/deployment.env

sed -e "s|__REPO__|$REPO_DIR|g" -e "s|__USER__|$USER_NAME|g" \
  "$REPO_DIR/deployment/t105-bot.service" | sudo tee /etc/systemd/system/t105-bot.service >/dev/null
sudo systemctl daemon-reload

# Pre-flight: read-only connectivity check with whatever keys are configured.
echo "Install complete. Next:"
echo "  1) sudo nano /etc/t105/deployment.env   (paste COMPETITION key + secret)"
echo "  2) sudo systemctl enable --now t105-bot"
echo "  3) journalctl -u t105-bot -f            (watch it run)"
echo "  4) bash deployment/healthcheck.sh"
