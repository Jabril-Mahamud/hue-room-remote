#!/usr/bin/env python3
"""A HueBleLight-shaped proxy backed by hue-ble-daemon.

A Hue bulb accepts exactly one BLE connection. Once hue-ble-daemon holds it,
anything that opens its own connection just fails -- which is every script
here, and the five hue-alarm timers with them. So when the daemon is running
these scripts go through it instead of around it, and the daemon stays the
single owner of the radio.

Only the members these scripts actually use are implemented; this is a
compatibility shim, not a reimplementation of HueBLE.
"""

import json
import os
import socket

SOCKET_NAME = "hue-ble.sock"
TIMEOUT = 60.0


def socket_path():
    runtime = os.environ.get("XDG_RUNTIME_DIR") or "/run/user/%d" % os.getuid()
    return os.path.join(runtime, SOCKET_NAME)


def daemon_running():
    return os.path.exists(socket_path())


def _request(payload):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.settimeout(TIMEOUT)
        s.connect(socket_path())
        s.sendall((json.dumps(payload) + "\n").encode())
        buf = b""
        while b"\n" not in buf:
            chunk = s.recv(65536)
            if not chunk:
                break
            buf += chunk
    reply = json.loads(buf.split(b"\n", 1)[0])
    if not reply.get("ok"):
        raise RuntimeError(reply.get("error", "daemon error"))
    return reply["state"]


class DaemonLight:
    """Mirrors the HueBleLight surface the scripts in this directory use.

    Every call returns the daemon's post-change state, so the cached
    properties are refreshed by writes as well as by poll_state() -- the
    same way the real object behaves.
    """

    def __init__(self):
        self._state = {}

    # -- connection lifecycle: the daemon owns it, so these are no-ops
    async def connect(self):
        self._state = _request({"op": "state"})
        return True

    async def disconnect(self):
        return True

    async def poll_state(self, *args, **kwargs):
        self._state = _request({"op": "state"})

    async def poll_brightness(self, *args, **kwargs):
        await self.poll_state()
        return self.brightness

    async def poll_effects(self, *args, **kwargs):
        await self.poll_state()
        return self.effect

    # -- state
    @property
    def power_state(self):
        return self._state.get("power")

    @property
    def brightness(self):
        return self._state.get("brightness")

    @property
    def colour_temp(self):
        return self._state.get("temp")

    @property
    def colour_xy(self):
        xy = self._state.get("xy")
        return tuple(xy) if xy else None

    @property
    def effect(self):
        """(EffectType, speed), matching HueBleLight -- callers index it."""
        effect = self._state.get("effect")
        if not effect:
            return None
        from HueBLE import EffectType
        try:
            return (EffectType[effect["name"]], int(effect["speed"]))
        except (KeyError, TypeError, ValueError):
            return (EffectType.NONE, 0)

    @property
    def connected(self):
        return True

    def _supports(self, key):
        return bool(self._state.get("supports", {}).get(key, True))

    @property
    def supports_effects(self):
        return self._supports("effects")

    @property
    def supports_colour_xy(self):
        return self._supports("colour_xy")

    @property
    def supports_colour_temp(self):
        return self._supports("colour_temp")

    # -- writes
    async def set_power(self, on):
        self._state = _request({"op": "set", "power": bool(on)})

    async def set_brightness(self, value):
        self._state = _request({"op": "set", "brightness": int(value)})

    async def set_colour_temp(self, value):
        self._state = _request({"op": "set", "temp": int(value)})

    async def set_colour_xy(self, x, y):
        self._state = _request({"op": "set", "xy": [float(x), float(y)]})

    # Argument order and names mirror HueBleLight exactly. They used to differ,
    # and hue-tui.py -- which passes HueBLE's own order -- raised TypeError on
    # every effect keypress for as long as the daemon was running.
    async def set_colour_effect(self, x, y, brightness, effect, effect_speed):
        self._state = _request({
            "op": "effect", "kind": "colour",
            "xy": [float(x), float(y)], "brightness": int(brightness),
            "name": getattr(effect, "name", str(effect)), "speed": int(effect_speed),
        })

    async def set_temperature_effect(self, colour_temp, brightness, effect, effect_speed):
        self._state = _request({
            "op": "effect", "kind": "temperature",
            "temp": int(colour_temp), "brightness": int(brightness),
            "name": getattr(effect, "name", str(effect)), "speed": int(effect_speed),
        })
