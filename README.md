# hue-room-remote

Omarchy / Quickshell bar widget for controlling Philips Hue lights by room,
over the bridge's local HTTP API (v1), or over Bluetooth LE direct to a
bulb when there is no bridge.

<p align="center">
  <img src="preview.png" alt="hue-room-remote panel screenshot" width="360">
</p>

## Features

- Bar icon (lightbulb) that opens a control panel
- **Room List** — shows every room with lights, each with an icon derived
  from its Hue-assigned room type (bedroom, office, kitchen, etc., falling
  back to a bulb icon where there's no good match) and a switch for on/off.
  Star a room to favorite it; if a favorite is set, the panel opens
  straight to that room instead of the list.
- **Room View** — tap a room to open it: toggle the whole room on/off
  (re-applying whatever scene was last used, rather than switching all
  lights "on" — this is remembered across restarts, not just for the
  current session), and tap a saved scene to apply it instantly.
- **Reorder and hide rooms** — tap the ⚙ in the header to reveal up/down
  arrows (for both the Room List and each room's scene list) and an eye
  icon to hide a room from the list entirely. Hidden rooms reappear
  (dimmed) while the ⚙ is active, so there's always a way to unhide one.
- Brightness changes apply to the whole room, not individual lights.
- Reads credentials from `~/.local/state/omarchy/settings/hue.json`
- Retries / re-fetches state automatically after every change

## Requirements

- Arch Linux + Omarchy (Quickshell-based shell)
- `curl`, `python3` (for the pairing script), `omarchy-shell`
- Bluetooth transport only: `bluez-utils`, and `bleak` + `HueBLE` for the
  daemon's interpreter

## Install

```sh
omarchy plugin add https://github.com/Jabril-Mahamud/hue-room-remote.git --enable
```

For a BLE bulb, also link the `hue` command line tools and the watchdog from
the plugin checkout, so `omarchy plugin update` keeps them current too:

```sh
~/.config/omarchy/plugins/lunardi0x01.hue-room-remote/install.sh
```

That puts everything in `cli/` on `~/bin`: `hue` (status, on/off, colour,
scenes), `hue-fade.py` (scene fades), `hue-tui.py`, `hue-alarms.py` (wake-up
timers). It also enables `hue-watchdog.timer`, which restarts the daemon when
the bulb stops answering, and seeds `~/.config/hue-scenes.json` from
`examples/` if you don't have one yet.

## Pairing with the bridge

On first run the bar shows a lightbulb icon. Click it to open the panel, then
click **Pair with bridge**. This opens a terminal — press the link button on
your Hue bridge when prompted. The script discovers the bridge, requests a
username, and writes `~/.local/state/omarchy/settings/hue.json`. The panel
picks up the new credentials automatically within seconds.

You can also pair manually:

```sh
~/.config/omarchy/plugins/lunardi0x01.hue-room-remote/pair.sh
```

Pass an IP directly to skip auto-discovery: `pair.sh 192.168.1.14`.

## Bluetooth LE (no bridge)

Hue bulbs sold since roughly 2019 speak Bluetooth as well as Zigbee, so a
single bulb can be driven without owning a bridge at all. Set it up with:

```sh
~/.config/omarchy/plugins/lunardi0x01.hue-room-remote/ble_setup.sh
```

It scans for bulbs, writes the one you pick into the same `hue.json` the
bridge path uses, and installs `hue-ble-daemon.service` as a systemd **user**
service. The panel then works as it always does -- the bulb appears as a
single room you can toggle, dim and apply scenes to.

If `bleak` and `HueBLE` live somewhere other than the system `python3` (a
venv, pyenv, or Homebrew python), point the setup script at that interpreter:

```sh
HUE_BLE_PYTHON=/path/to/python3 ./ble_setup.sh
```

### Why there is a daemon

A Hue bulb accepts exactly one BLE connection, and a cold
scan-connect-read costs 5-14 s. Spawning a fresh connection per panel click
would make the UI unusable and would lock every other tool out of the bulb.
`hue_ble_daemon.py` holds one connection open and serves it over a unix
socket in `$XDG_RUNTIME_DIR`, which puts a panel action at about 1 s and a
cached read at ~40 ms. It also means anything else on the machine can drive
the same bulb through the daemon instead of fighting it for the radio.

### Scenes

There are no bridge-stored scenes without a bridge, so scenes come from
`~/.config/hue-scenes.json`:

```json
{
  "evening":   { "section": "Evening", "colour": "orange", "brightness": 60 },
  "day":       { "temp": 250, "brightness": 254 },
  "night":     { "temp": 500, "brightness": 12 },
  "off":       { "power": false },
  "fireplace": { "colour": "orange", "brightness": 110, "effect": "fireplace" },
  "surprise":  { "shuffle": true }
}
```

Each key becomes a scene in the panel. `temp` is in mireds (153 cool - 500
warm), `brightness` is 1-254, and `colour` is one of the names in
`hue_ble_daemon.py`'s `COLOURS` table. Any scene that sets something other
than `power` implies the light should be on.

`section` is the panel heading the scene sits under; sections appear in the
order the file first mentions them. Scenes without one go under "Effects" if
they carry an effect, "Quick actions" if they're an off, flash or shuffle, and
"Scenes" otherwise. `examples/hue-scenes.json` is a full example.

A scene may also carry an `effect` (any name in `EffectType`, with an optional
`speed` 0-255), which is written together with the colour and brightness in one
go, or be `{"shuffle": true}`, which resolves to a random one of the other
scenes - skipping power-off and flash scenes.

### Switching back to a bridge

Delete `"transport"` from `~/.local/state/omarchy/settings/hue.json` and
`systemctl --user disable --now hue-ble-daemon`. Bridge credentials in that
file are left untouched by BLE setup, so nothing needs re-pairing.

### Limitations

- One bulb per install. The panel's room model is synthesised from that
  single light, so grouping several BLE bulbs is not supported.
- Bluetooth range only, and the bulb must be mains-powered to be reachable
  -- a bulb switched off at the wall does not advertise.
- A bulb still connected to the Hue phone app will not accept the daemon's
  connection; close the app first.

## Remove

```sh
~/.config/omarchy/plugins/lunardi0x01.hue-room-remote/cleanup.sh
omarchy plugin remove lunardi0x01.hue-room-remote
```

The cleanup script removes your bridge credentials from
`~/.local/state/omarchy/settings/hue.json`. Run it before removing the
plugin so no auth token is left behind.

## Notes

- The Bluetooth transport bypasses the bridge entirely and touches none of
  the TLS/pairing machinery below; `transport: "ble"` in `hue.json` is what
  selects it, and the panel's own QML is shared by both transports.
- Speaks to the bridge over **HTTPS** on your LAN with the v1 API
  (`/api/<username>/lights`, `/groups`, etc.) — no cloud, no SDK.
- TLS is verified with the bundled `hue_bridge_cacert.pem`, the official
  Philips Hue root CA from Signify. During pairing, the bridge's unique ID
  is read from `/api/config` and saved as `bridgeId` in `hue.json`; requests
  are then addressed to that ID so the bridge certificate's hostname is
  matched, while the connection itself goes straight to the bridge's IP.
- Automatic discovery uses Philips' hosted lookup once; pass an IP directly
  to `pair.sh` to skip it.
- If `bridgeId` is missing (e.g. from an older config), the panel warns
  "TLS verification disabled" — re-run `pair.sh` to restore full
  certificate verification.
- Uses the classic v1 local API, which every current bridge still serves —
  including the 2025 Bridge Pro (HTTPS-only, `apiversion` 1.73.x). New Hue
  features ship exclusively in the v2 API and Signify has said v1 will be
  removed long-term, but no end-of-life date has been announced.
- Credentials are stored per-user in `~/.local/state/omarchy/settings/hue.json`;
  keep that file out of version control.

## Credit

This started as a fork of [sethchev/omarchy-philips-hue](https://github.com/sethchev/omarchy-philips-hue),
which laid down the original bridge-pairing and TLS groundwork. It's since
been rewritten with claude code — new room-based UI, bug fixes, an added
test suite — and renamed to reflect that. Both projects are MIT licensed;
see [LICENSE](LICENSE).
