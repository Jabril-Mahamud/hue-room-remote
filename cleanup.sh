#!/usr/bin/env bash
set -euo pipefail

STATE_FILE="${XDG_STATE_HOME:-$HOME/.local/state}/omarchy/settings/hue.json"

removed=0

secure_remove() {
  local f="$1"
  [[ -e "$f" || -L "$f" ]] || return 0
  if [[ -d "$f" && ! -L "$f" ]]; then
    return 0  # a real directory at this path isn't ours to remove
  fi
  # One open (O_NOFOLLOW) and everything else checked against that same
  # descriptor -- not a path-based -L/-f test followed by a separate
  # lstat()+open() pair, which leaves a window for the path to be swapped
  # between checks.
  python3 -c "
import os, stat, sys
path = sys.argv[1]
try:
    fd = os.open(path, os.O_WRONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
except OSError:
    sys.exit(0)  # symlink, or otherwise not safely writable in place
try:
    st = os.fstat(fd)
    if stat.S_ISREG(st.st_mode) and st.st_uid == os.getuid() and st.st_size > 0:
        os.write(fd, b'\x00' * st.st_size)
finally:
    os.close(fd)
" "$f" 2>/dev/null || true
  rm -f "$f"
}

if [[ -f "$STATE_FILE" ]]; then
  secure_remove "$STATE_FILE"
  if [[ ! -e "$STATE_FILE" && ! -L "$STATE_FILE" ]]; then
    echo "Removed $STATE_FILE"
    removed=$((removed + 1))
  fi
fi

# The BLE transport leaves a systemd user service behind as well as the
# credentials file, and a daemon still holding the bulb's one connection
# would lock out anything installed after this.
UNIT="$HOME/.config/systemd/user/hue-ble-daemon.service"
if [[ -f "$UNIT" ]]; then
  systemctl --user disable --now hue-ble-daemon.service >/dev/null 2>&1 || true
  rm -f "$UNIT"
  systemctl --user daemon-reload >/dev/null 2>&1 || true
  echo "Removed $UNIT"
  removed=$((removed + 1))
fi

if [[ $removed -eq 0 ]]; then
  echo "No Hue files found to remove."
fi
