#!/usr/bin/env bash
# Safe copy of the Flip for Free database (works while the app is running).
# Keeps 30 days locally. If you've set up rclone with a remote called "gdrive",
# each backup is also copied to Google Drive (folder: Flip for Free backups).
set -euo pipefail
DATA_DIR="${FLIP_DATA_DIR:-$HOME/flip-data}"
DB="$DATA_DIR/ebay_profit_tracker.db"
OUT="$DATA_DIR/backups/flip-$(date +%Y-%m-%d_%H%M).db"
mkdir -p "$DATA_DIR/backups"
python3 - "$DB" "$OUT" <<'PY'
import sqlite3, sys
src, dst = sqlite3.connect(sys.argv[1]), sqlite3.connect(sys.argv[2])
src.backup(dst)          # consistent copy even while Flip for Free is writing
dst.close(); src.close()
PY
gzip -f "$OUT"
find "$DATA_DIR/backups" -name 'flip-*.db.gz' -mtime +30 -delete
if command -v rclone >/dev/null && rclone listremotes 2>/dev/null | grep -q '^gdrive:'; then
  rclone copy "$OUT.gz" "gdrive:Flip for Free backups" && echo "Copied to Google Drive"
fi
echo "Backed up to $OUT.gz"
