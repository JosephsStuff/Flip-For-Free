#!/usr/bin/env bash
# Flip for Free - install / update on an Ubuntu server (e.g. Oracle Cloud Always Free).
# Run from the unzipped folder:   bash flip-for-free/deploy/install.sh
# Safe to run again after uploading a new version: your data in ~/flip-data is kept.
set -euo pipefail

REPO_URL="${FLIP_REPO:-https://github.com/JosephsStuff/flip-for-free.git}"
DATA_DIR="${FLIP_DATA_DIR:-$HOME/flip-data}"

# Run straight from GitHub (curl … | bash) or from an unzipped copy.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd || echo "")"
if [ -n "$SCRIPT_DIR" ] && [ -f "$SCRIPT_DIR/../app.py" ]; then
  APP_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
else
  APP_DIR="$HOME/flip-for-free"
  sudo apt-get update -qq && sudo apt-get install -y -qq git >/dev/null
  if [ -d "$APP_DIR/.git" ]; then git -C "$APP_DIR" pull --ff-only; else git clone "$REPO_URL" "$APP_DIR"; fi
fi
USER_NAME="$(id -un)"
PORT=8765

say() { printf '\n\033[1;32m==> %s\033[0m\n' "$*"; }

say "Installing system packages"
sudo apt-get update -qq
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq python3 python3-venv python3-pip git curl unzip >/dev/null

say "Setting up Python environment"
python3 -m venv "$APP_DIR/.venv"
"$APP_DIR/.venv/bin/pip" install -q --upgrade pip
"$APP_DIR/.venv/bin/pip" install -q -r "$APP_DIR/requirements.txt"

say "Preparing your data folder: $DATA_DIR"
mkdir -p "$DATA_DIR/backups"
DB="$DATA_DIR/ebay_profit_tracker.db"
"$APP_DIR/.venv/bin/python" - "$DB" <<PY
import sys; sys.path.insert(0, "$APP_DIR")
import db
db.init_db(sys.argv[1])          # creates it, or upgrades an existing one you uploaded
db.set_db_path(sys.argv[1])
print("   database:", sys.argv[1])
PY

say "Installing Tailscale (private, encrypted access from your own devices)"
if ! command -v tailscale >/dev/null; then
  curl -fsSL https://tailscale.com/install.sh | sh
fi
if ! tailscale status >/dev/null 2>&1; then
  if [ -n "${TS_AUTHKEY:-}" ]; then
    # unattended (e.g. Oracle cloud-init): a one-off auth key from Tailscale's admin console
    sudo tailscale up --hostname=flip --auth-key="$TS_AUTHKEY"
  else
    echo "   Open the link below in your browser and sign in to Tailscale to add this server:"
    sudo tailscale up --hostname=flip
  fi
fi
TS_NAME="$(tailscale status --json | "$APP_DIR/.venv/bin/python" -c 'import json,sys; print(json.load(sys.stdin)["Self"]["DNSName"].rstrip("."))')"
PUBLIC_URL="https://$TS_NAME"
echo "   Flip for Free will be at: $PUBLIC_URL"

say "Creating the Flip for Free service (starts on boot, restarts if it stops)"
sudo tee /etc/systemd/system/flip.service >/dev/null <<UNIT
[Unit]
Description=Flip for Free
After=network-online.target tailscaled.service
Wants=network-online.target

[Service]
User=$USER_NAME
WorkingDirectory=$APP_DIR
Environment=PYTHONUNBUFFERED=1
ExecStart=$APP_DIR/.venv/bin/python app.py --port $PORT --public-url $PUBLIC_URL
# exit code 3 = restart after an in-app update
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
UNIT

say "Nightly backups (kept for 30 days in $DATA_DIR/backups)"
chmod +x "$APP_DIR/deploy/backup.sh"
sudo tee /etc/systemd/system/flip-backup.service >/dev/null <<UNIT
[Unit]
Description=Back up the Flip for Free database
[Service]
Type=oneshot
User=$USER_NAME
Environment=FLIP_DATA_DIR=$DATA_DIR
ExecStart=$APP_DIR/deploy/backup.sh
UNIT
sudo tee /etc/systemd/system/flip-backup.timer >/dev/null <<UNIT
[Unit]
Description=Back up Flip for Free every night
[Timer]
OnCalendar=*-*-* 03:30:00
Persistent=true
[Install]
WantedBy=timers.target
UNIT

sudo systemctl daemon-reload
sudo systemctl enable --now flip.service flip-backup.timer
sudo systemctl restart flip.service

say "Publishing it privately on your tailnet with https"
sudo tailscale serve --bg $PORT || {
  echo "   Couldn't switch on Tailscale Serve automatically. In the Tailscale admin console, go to DNS and"
  echo "   enable MagicDNS and HTTPS Certificates, then run:  sudo tailscale serve --bg $PORT"
}

sleep 3
if systemctl is-active --quiet flip.service; then
  say "Done!"
  cat <<MSG

   Open  $PUBLIC_URL  on any device signed in to your Tailscale account.

   eBay sign-in: on developer.ebay.com → User Tokens → your RuName, set BOTH
     "Your auth accepted URL" and "Your auth declined URL" to:
       $PUBLIC_URL/auth/callback

   Useful commands:
     sudo systemctl status flip        is it running?
     journalctl -u flip -f             live log
     sudo systemctl restart flip       restart
     bash $APP_DIR/deploy/backup.sh    back up now
   Updates: Settings → Updates in Flip for Free (or run this script again).
MSG
else
  echo "Flip for Free didn't start. See: journalctl -u flip -n 50"
  exit 1
fi
