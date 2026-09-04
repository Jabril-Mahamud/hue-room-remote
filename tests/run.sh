#!/usr/bin/env bash
set -uo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.."

status=0

echo "== Python (hue_api.py) =="
python3 -m unittest tests.test_hue_api -v || status=1

echo
echo "== Python (hue_ble.py) =="
python3 -m unittest tests.test_hue_ble -v || status=1

echo
echo "== JS (hue_api.js) =="
node --test tests/hue_api.test.js || status=1

echo
echo "== Bash (pair.sh valid_ip) =="
bash tests/test_pair_sh.sh || status=1

echo
echo "== Bash (cleanup.sh secure_remove) =="
bash tests/test_cleanup_sh.sh || status=1

exit $status
