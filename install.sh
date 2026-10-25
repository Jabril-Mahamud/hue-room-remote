#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")"

# Links the hue CLI and watchdog out of this checkout, so `omarchy plugin
# update` (a git pull here) updates them too. The BLE daemon unit is not
# handled here: ble_setup.sh writes it, since it needs the pairing step.
REPO="$PWD"
BIN="$HOME/bin"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
SCENES="${XDG_CONFIG_HOME:-$HOME/.config}/hue-scenes.json"

mkdir -p "$BIN" "$UNIT_DIR"

for f in cli/*; do
  # Tests run in place; they don't belong on PATH.
  case "$(basename "$f")" in test_*) continue ;; esac
  ln -sfn "$REPO/$f" "$BIN/$(basename "$f")"
done

for f in systemd/*; do
  ln -sfn "$REPO/$f" "$UNIT_DIR/$(basename "$f")"
done

# Only seeded when missing: it's the user's file from then on.
[ -e "$SCENES" ] || cp examples/hue-scenes.json "$SCENES"

systemctl --user daemon-reload
systemctl --user enable --now hue-watchdog.timer

cat <<EOF
Linked $(ls cli | grep -vc '^test_') commands into $BIN and the watchdog units into $UNIT_DIR.
Scenes: $SCENES
EOF
