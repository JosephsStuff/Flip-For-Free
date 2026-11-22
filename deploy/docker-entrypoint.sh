#!/bin/sh
# Start Flip for Free inside its container.
#  * The app runs from /data/app (a copy of the image's /opt/flip), so in-app updates persist.
#    A newer image replaces that copy; an older or equal one leaves your in-app updates alone.
#  * Your database and settings stay in /data and are never touched here.
#  * Exit code 3 means "restart after an update": the loop below starts it again.
set -e
IMAGE=/opt/flip
APP=/data/app
mkdir -p /data/config /data/backups

ver() { cat "$1/VERSION" 2>/dev/null || echo 0.0.0; }
newer() {   # is version $1 newer than $2 ?
  python3 - "$1" "$2" <<'PY'
import sys
t = lambda v: tuple(int("".join(c for c in p if c.isdigit()) or 0) for p in (v.strip().lstrip("v").split(".") + ["0", "0"])[:3])
sys.exit(0 if t(sys.argv[1]) > t(sys.argv[2]) else 1)
PY
}

if [ ! -f "$APP/app.py" ] || newer "$(ver $IMAGE)" "$(ver $APP)"; then
  echo "Flip for Free: installing version $(ver $IMAGE) into $APP"
  rm -rf "$APP.new"
  cp -a "$IMAGE" "$APP.new"
  rm -rf "$APP"
  mv "$APP.new" "$APP"
fi

# An in-app update may need packages the image doesn't have yet.
if ! cmp -s "$APP/requirements.txt" "$IMAGE/requirements.txt"; then
  pip install -q --no-cache-dir -r "$APP/requirements.txt" || echo "Flip for Free: couldn't install new requirements"
fi

# First start: put the database in /data.
python3 - <<'PY'
import os, sys
sys.path.insert(0, "/data/app")
import db
if not db.get_db_path():
    p = os.path.join(os.environ.get("FLIP_DATA_DIR", "/data"), db.DB_FILENAME)
    db.init_db(p)
    db.set_db_path(p)
    print("Flip for Free: database at", p)
PY

child=0
trap 'kill -TERM "$child" 2>/dev/null; wait "$child"; exit 0' TERM INT
while true; do
  python3 "$APP/app.py" --port "${FLIP_PORT:-8765}" "$@" &
  child=$!
  set +e; wait "$child"; code=$?; set -e
  [ "$code" -eq 3 ] || exit "$code"
  echo "Flip for Free: restarting after an update"
done
