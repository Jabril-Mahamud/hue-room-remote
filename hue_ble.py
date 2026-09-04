#!/usr/bin/env python3
"""Bluetooth LE transport: daemon client plus bridge-shaped JSON synthesis.

A Hue bulb reachable over BLE has no bridge, and therefore none of the
concepts panel.qml is built around -- no rooms, no groups, no bridge-stored
scenes. Rather than teach the panel a second data model, this module dresses
a single BLE bulb up in the v1 shapes the panel already parses: one light,
one synthetic room containing it, and scenes read from a local file. The QML
side is untouched by the BLE transport.

Everything here is pure/stdlib except `request()`, which talks to
hue_ble_daemon.py over a unix socket. The daemon exists because a BLE
connect-and-read costs 5-14 s, which no click-driven UI can absorb; it holds
one connection open so these calls are milliseconds.
"""

import json
import os
import re
import socket

# The panel addresses lights/groups by id and hue_api.py restricts put-* ids
# to digits, so the synthetic ids have to be numeric strings.
LIGHT_ID = "1"
GROUP_ID = "1"

SOCKET_NAME = "hue-ble.sock"
# Must exceed the daemon's reconnect wait plus a connect, or a caller
# times out exactly when the daemon is busy recovering the link.
DEFAULT_TIMEOUT = 45.0

# Same ceiling as a bridge reply. The daemon is local and trusted, but a
# wedged writer shouldn't be able to grow this process without bound.
MAX_REPLY_BYTES = 1024 * 1024

MAX_SCENES = 100

_ADDRESS_RE = re.compile(r'^(?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}$')
_SCENE_ID_RE = re.compile(r'^[a-zA-Z0-9_-]{1,40}$')

BRI_MIN, BRI_MAX = 1, 254
CT_MIN, CT_MAX = 153, 500


def socket_path():
    runtime = os.environ.get("XDG_RUNTIME_DIR") or "/run/user/%d" % os.getuid()
    return os.path.join(runtime, SOCKET_NAME)


def scenes_path():
    config_home = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return os.path.join(config_home, "hue-scenes.json")


def ble_config(creds):
    """Extract and validate the `ble` block from hue.json, or None.

    Returning None (rather than raising) for anything malformed means a
    half-written config degrades to "not paired" in the panel instead of
    breaking the bridge path for users who never enabled BLE.
    """
    if not isinstance(creds, dict):
        return None
    if creds.get("transport") != "ble":
        return None
    ble = creds.get("ble")
    if not isinstance(ble, dict):
        return None
    address = str(ble.get("address") or "").strip().upper()
    if not _ADDRESS_RE.match(address):
        return None
    return {
        "address": address,
        "name": str(ble.get("name") or "Hue light")[:200],
        # Drives which Nerd Font glyph the panel picks for the room; any
        # unknown value falls back to its bulb icon, so no validation list.
        "roomClass": str(ble.get("roomClass") or "Other")[:200],
    }


def _clamp(value, low, high, default):
    try:
        return max(low, min(high, int(value)))
    except (TypeError, ValueError):
        return default


def lights_payload(state, cfg):
    """One light, in GET /lights shape (hue_api.js roomBrightness reads .state.bri)."""
    light = {
        "state": {
            "on": bool(state.get("power")),
            "bri": _clamp(state.get("brightness"), BRI_MIN, BRI_MAX, BRI_MAX),
            "reachable": bool(state.get("reachable", True)),
        },
        "name": cfg["name"],
        "type": "Extended color light",
        "uniqueid": cfg["address"],
    }
    ct = state.get("temp")
    if ct is not None:
        light["state"]["ct"] = _clamp(ct, CT_MIN, CT_MAX, CT_MIN)
    xy = state.get("xy")
    if _valid_xy(xy):
        light["state"]["xy"] = [xy[0], xy[1]]
    return {LIGHT_ID: light}


def groups_payload(state, cfg):
    """One synthetic Room holding that light, in GET /groups shape."""
    on = bool(state.get("power"))
    bri = _clamp(state.get("brightness"), BRI_MIN, BRI_MAX, BRI_MAX)
    return {
        GROUP_ID: {
            "name": cfg["name"],
            "type": "Room",
            "class": cfg["roomClass"],
            "lights": [LIGHT_ID],
            "state": {"all_on": on, "any_on": on},
            "action": {"on": on, "bri": bri},
        }
    }


def scenes_payload(scenes):
    """Local scene definitions in GET /scenes shape, all bound to the room.

    Scene *names* are the ids here: they come from a file the user writes by
    hand, and a stable, readable id is worth more than a generated one when
    the same file is also driven from the command line.
    """
    out = {}
    if not isinstance(scenes, dict):
        return out
    for name in sorted(scenes):
        if len(out) >= MAX_SCENES:
            break
        if not _SCENE_ID_RE.match(str(name)):
            continue
        if not isinstance(scenes[name], dict):
            continue
        out[str(name)] = {"name": str(name), "group": GROUP_ID}
    return out


def load_scenes():
    try:
        with open(scenes_path(), "rb") as f:
            data = f.read(MAX_REPLY_BYTES + 1)
        if len(data) > MAX_REPLY_BYTES:
            return {}
        parsed = json.loads(data)
        return parsed if isinstance(parsed, dict) else {}
    except Exception:
        return {}


def _valid_xy(xy):
    """True for a real chromaticity, false for the bulb's (1.0, 1.0) sentinel.

    In colour-temperature mode the firmware reports 0xFFFF for both
    coordinates. Passing that back into a write makes the bulb reject the
    whole command, so it must never reach a payload.
    """
    if not isinstance(xy, (list, tuple)) or len(xy) != 2:
        return False
    try:
        x, y = float(xy[0]), float(xy[1])
    except (TypeError, ValueError):
        return False
    return x > 0 and y > 0 and x + y <= 1.0


def group_action_to_request(action):
    """Translate a v1 group action from the panel into a daemon request.

    The panel only ever sends {on}, {bri} or {scene} (panel.qml's
    toggleRoom/setBrightness/applyScene); ct/xy are accepted too so the same
    entry point serves command-line callers.
    """
    if not isinstance(action, dict):
        return None
    if "scene" in action:
        scene = str(action["scene"])
        if not _SCENE_ID_RE.match(scene):
            return None
        return {"op": "scene", "name": scene}
    req = {"op": "set"}
    if "on" in action:
        req["power"] = bool(action["on"])
    if "bri" in action:
        req["brightness"] = _clamp(action["bri"], BRI_MIN, BRI_MAX, BRI_MAX)
    if "ct" in action:
        req["temp"] = _clamp(action["ct"], CT_MIN, CT_MAX, CT_MIN)
    if _valid_xy(action.get("xy")):
        req["xy"] = [float(action["xy"][0]), float(action["xy"][1])]
    if len(req) == 1:
        return None
    return req


def request(payload, timeout=DEFAULT_TIMEOUT):
    """Send one JSON-line request to the daemon and return its reply.

    Raises on connect/timeout/protocol failure so hue_api.py's dispatch can
    exit non-zero -- panel.qml distinguishes "backend didn't answer" from
    "nothing to show" purely by exit code.
    """
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.settimeout(timeout)
        s.connect(socket_path())
        s.sendall((json.dumps(payload) + "\n").encode())
        chunks = []
        total = 0
        while True:
            chunk = s.recv(65536)
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_REPLY_BYTES:
                raise ValueError("daemon reply too large")
            chunks.append(chunk)
            if b"\n" in chunk:
                break
    reply = json.loads(b"".join(chunks).split(b"\n", 1)[0])
    if not isinstance(reply, dict) or not reply.get("ok"):
        raise RuntimeError(str(reply.get("error", "daemon error"))[:200])
    return reply
