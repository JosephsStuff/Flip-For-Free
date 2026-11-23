#!/bin/bash
cd "$(dirname "$0")"
export FLIP_LAUNCHER=1
echo ""
echo "  Getting Flip for Free ready (the first time can take a minute)..."
python3 -m pip install -q -r requirements.txt
while true; do
  python3 app.py --lan "$@"
  [ $? -eq 3 ] || break   # exit code 3 = restart after an update
done
