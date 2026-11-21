#!/bin/bash
cd "$(dirname "$0")"
export FLIP_LAUNCHER=1
python3 -m pip install -q -r requirements.txt
while true; do
  python3 app.py "$@"
  [ $? -eq 3 ] || break   # exit code 3 = restart after an update
done
