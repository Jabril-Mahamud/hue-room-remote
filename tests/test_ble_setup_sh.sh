#!/usr/bin/env bash
# Exercises ble_setup.sh's valid_mac() in isolation (the script pairs live at
# the top level, so the function is pulled out with sed, as in test_pair_sh.sh),
# and checks .env.example parses and passes it.
set -uo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
eval "$(sed -n '/^valid_mac()/p' "$SCRIPT_DIR/../ble_setup.sh")"

fail=0
check() { # expected(0|1) mac
  if valid_mac "$2"; then got=0; else got=1; fi
  if [ "$got" = "$1" ]; then echo "ok   valid_mac('$2')"; else echo "FAIL valid_mac('$2')"; fail=1; fi
}

check 0 "AA:BB:CC:DD:EE:FF"
check 0 "aa:bb:cc:00:11:22"
check 1 ""
check 1 "AA:BB:CC:DD:EE"
check 1 "AA:BB:CC:DD:EE:FF:00"
check 1 "AA-BB-CC-DD-EE-FF"
check 1 "AA:BB:CC:DD:EE:FG"

( . "$SCRIPT_DIR/../.env.example" && valid_mac "$HUE_BLE_ADDRESS" && [ -n "$HUE_ROOM_NAME" ] ) \
  && echo "ok   .env.example parses" || { echo "FAIL .env.example"; fail=1; }

exit $fail
