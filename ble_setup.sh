#!/usr/bin/env bash
# Bluetooth LE setup: find a Hue bulb, record it in hue.json, start the daemon.
#
# The BLE counterpart to pair.sh. There is no bridge and no username to
# request -- "pairing" here is discovering the bulb's address, letting BlueZ
# bond with it, and pointing the plugin at it.
set -euo pipefail

PLUGIN_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STATE_DIR="${XDG_STATE_HOME:-$HOME/.local/state}/omarchy/settings"
CREDS="$STATE_DIR/hue.json"
UNIT_DIR="$HOME/.config/systemd/user"
PYTHON="${HUE_BLE_PYTHON:-python3}"

die() { echo "error: $*" >&2; exit 1; }

command -v bluetoothctl >/dev/null || die "bluetoothctl not found (install bluez-utils)"
"$PYTHON" -c 'import bleak, HueBLE' 2>/dev/null \
  || die "$PYTHON is missing bleak/HueBLE. Install them, or set HUE_BLE_PYTHON to an interpreter that has them."

echo "Scanning for Hue bulbs (20s)..."
# Match on the Philips/Signify service UUID (fe0f) rather than the device
# name: a bulb that has been factory reset advertises under a rotating
# private address with no name attached until it is connected to.
mapfile -t FOUND < <("$PYTHON" - <<'PY'
import asyncio
from bleak import BleakScanner

async def main():
    seen = {}
    def cb(d, ad):
        uuids = [u.lower() for u in ad.service_uuids]
        if any("fe0f" in u or "932c32bd" in u for u in uuids) or (d.name or "").lower().startswith("hue"):
            seen[d.address] = (d.name or "Hue light", ad.rssi)
    scanner = BleakScanner(cb)
    await scanner.start()
    await asyncio.sleep(20)
    await scanner.stop()
    for addr, (name, rssi) in sorted(seen.items(), key=lambda kv: -kv[1][1]):
        print("%s\t%s\t%s" % (addr, name, rssi))

asyncio.run(main())
PY
)

[ "${#FOUND[@]}" -gt 0 ] || die "no Hue bulbs found. Check the bulb has mains power, and close the Hue phone app -- a bulb already connected to it stops advertising."

echo
echo "Found:"
for i in "${!FOUND[@]}"; do
  IFS=$'\t' read -r addr name rssi <<<"${FOUND[$i]}"
  printf "  [%d] %s  %s  (rssi %s)\n" "$((i + 1))" "$addr" "$name" "$rssi"
done
echo
read -rp "Which bulb? [1] " choice
choice="${choice:-1}"
[[ "$choice" =~ ^[0-9]+$ ]] && [ "$choice" -ge 1 ] && [ "$choice" -le "${#FOUND[@]}" ] \
  || die "invalid selection"
IFS=$'\t' read -r ADDRESS DEFAULT_NAME _ <<<"${FOUND[$((choice - 1))]}"

read -rp "Room name to show in the panel? [$DEFAULT_NAME] " ROOM_NAME
ROOM_NAME="${ROOM_NAME:-$DEFAULT_NAME}"
read -rp "Room type (Bedroom, Office, Living room, ...)? [Other] " ROOM_CLASS
ROOM_CLASS="${ROOM_CLASS:-Other}"

echo "Trusting $ADDRESS..."
# No explicit `pair` -- BlueZ bonds on the first encrypted GATT access, and
# an unprompted pair on a bulb still bonded to a phone often just fails.
bluetoothctl trust "$ADDRESS" >/dev/null || true

mkdir -p "$STATE_DIR"
ADDRESS="$ADDRESS" ROOM_NAME="$ROOM_NAME" ROOM_CLASS="$ROOM_CLASS" CREDS="$CREDS" \
  "$PYTHON" - <<'PY'
import json, os

path = os.environ["CREDS"]
try:
    with open(path) as f:
        creds = json.load(f)
    if not isinstance(creds, dict):
        creds = {}
except Exception:
    creds = {}

# Any existing bridge credentials are left in place, so switching back is
# just a matter of removing "transport".
creds["transport"] = "ble"
creds["ble"] = {
    "address": os.environ["ADDRESS"],
    "name": os.environ["ROOM_NAME"],
    "roomClass": os.environ["ROOM_CLASS"],
}

fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
with os.fdopen(fd, "w") as f:
    json.dump(creds, f, indent=2)
    f.write("\n")
print("wrote", path)
PY

mkdir -p "$UNIT_DIR"
sed "s|^ExecStart=.*|ExecStart=$PYTHON $PLUGIN_DIR/hue_ble_daemon.py|" \
  "$PLUGIN_DIR/hue-ble-daemon.service" > "$UNIT_DIR/hue-ble-daemon.service"
systemctl --user daemon-reload
systemctl --user enable --now hue-ble-daemon.service

echo
echo "Done. Daemon status:"
systemctl --user --no-pager --lines=0 status hue-ble-daemon.service || true
echo
echo "The panel picks up the new transport within a few seconds."
