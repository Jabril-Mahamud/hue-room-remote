#!/usr/bin/env python3
"""Holds one BLE connection to a Hue bulb open and serves it over a socket.

Without this, every panel click pays a fresh scan-connect-read cycle: 5 s
warm, 14 s cold on the hardware this was written against. The bulb also
accepts only one BLE connection at a time, so a daemon is the only way for
the panel, a CLI and a keybind to share it.

Protocol: one JSON object per line in, one JSON object per line out.
  {"op": "state"}                          -> current state
  {"op": "set", "power": true, "bri": 200} -> apply, then current state
  {"op": "scene", "name": "evening"}       -> apply a scene from hue-scenes.json

Every reply is {"ok": true, "state": {...}} or {"ok": false, "error": "..."}.
"""

import asyncio
import json
import os
import sys
import time

from bleak import BleakScanner
from HueBLE import HueBleLight, EffectType

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import hue_ble

# Shipping firmware reports effect ids HueBLE's enum doesn't define (0x0D is
# one). EffectType(raw) then raises inside poll_effects and the light reads
# as unreachable until something writes a known effect back, so unknown ids
# are treated as "no effect" rather than as an error.
EffectType._missing_ = classmethod(lambda cls, value: cls.NONE)

CREDS_FILE = os.path.join(
    os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state"),
    "omarchy", "settings", "hue.json",
)

SCAN_TIMEOUT = 10.0
CT_MIN, CT_MAX = 153, 500
# How long to let HueBLE's own reconnect finish before taking over.
RECONNECT_WAIT = 8.0
RECONNECT_POLL = 0.5
STATE_CACHE_SECONDS = 1.5
# Colour names are resolved to CIE xy here so a scene file can say "red"
# instead of carrying coordinates. Hand-tuned against a Hue colour lamp --
# the mathematically correct sRGB conversion lands visibly off on this
# firmware, so these are measured values, not computed ones.
COLOURS = {
    "red": (0.67, 0.30), "orange": (0.58, 0.38), "yellow": (0.45, 0.48),
    "green": (0.21, 0.65), "blue": (0.15, 0.06), "purple": (0.28, 0.12),
    "pink": (0.40, 0.22), "white": (0.32, 0.34), "warm": (0.45, 0.40),
    "cool": (0.30, 0.32), "amber": (0.55, 0.42),
}


class Bulb:
    """One bulb, one long-lived connection, one lock.

    HueBleLight owns its own recovery: on an unexpected disconnect it starts
    a background reconnect task of its own. Racing that with a second
    connect (or a freshly constructed HueBleLight for the same device) is
    what BlueZ reports as org.bluez.Error.InProgress, and once that starts
    the bulb stays unreachable until something backs off. So this keeps a
    single instance and waits for its recovery rather than competing with
    it, only rebuilding if that recovery has clearly given up.
    """

    def __init__(self, address):
        self.address = address
        self.light = None
        # Serialises BLE access: two bar instances, a CLI and a keybind can
        # all be in flight at once, and writes to one bulb cannot interleave.
        self.lock = asyncio.Lock()
        self._cached_state = None
        self._cached_at = 0.0

    async def _drop(self):
        """Tear the connection down so the next attempt starts clean.

        disconnect() sets HueBLE's _expect_disconnect, which is what stops
        it from spawning yet another background reconnect behind us.
        """
        light, self.light = self.light, None
        if light is None:
            return
        try:
            await light.disconnect()
        except Exception:
            pass

    async def _connect_once(self):
        device = await BleakScanner.find_device_by_address(
            self.address, timeout=SCAN_TIMEOUT
        )
        if device is None:
            raise RuntimeError("light not found at %s" % self.address)
        light = HueBleLight(device)
        await light.connect()
        self.light = light

    async def _ensure_connected(self):
        if self.light is None:
            await self._connect_once()
            return
        if self.light.connected:
            return

        # Its own reconnect task is most likely already in flight; let it
        # finish rather than issuing a competing connect.
        deadline = time.monotonic() + RECONNECT_WAIT
        while time.monotonic() < deadline:
            await asyncio.sleep(RECONNECT_POLL)
            if self.light is not None and self.light.connected:
                return

        await self._drop()
        await self._connect_once()

    async def _state(self, force):
        """Current state, reusing a very recent poll if there is one.

        Every bar instance polls independently (one per monitor), and the
        panel follows a group fetch with a lights fetch -- without this, one
        refresh becomes four BLE round trips that all report the same thing.
        The window is far shorter than the panel's poll interval, so a real
        change still shows up on the next cycle.
        """
        if (
            not force
            and self._cached_state is not None
            and time.monotonic() - self._cached_at < STATE_CACHE_SECONDS
        ):
            return self._cached_state

        await self.light.poll_state()
        xy = self.light.colour_xy
        # HueBleLight.effect is a (EffectType, speed) tuple, not an enum.
        temp = self.light.colour_temp
        effect = getattr(self.light, "effect", None)
        state = {
            "power": bool(self.light.power_state),
            "brightness": self.light.brightness,
            # In colour mode the firmware reports 0xFFFF here, the same
            # sentinel it uses for xy in temperature mode. Clamping that
            # into range would report a confident, wrong 500 mireds.
            "temp": temp if temp is not None and CT_MIN <= temp <= CT_MAX else None,
            "xy": list(xy) if hue_ble._valid_xy(xy) else None,
            "reachable": True,
            # Reported so command-line clients can proxy the whole light
            # through this daemon instead of opening a second connection --
            # the bulb only accepts one, so anything that connects directly
            # while the daemon is running simply fails.
            "effect": (
                {"name": effect[0].name, "speed": effect[1]}
                if effect is not None
                else None
            ),
            "supports": {
                "effects": bool(getattr(self.light, "supports_effects", False)),
                "colour_xy": bool(getattr(self.light, "supports_colour_xy", False)),
                "colour_temp": bool(getattr(self.light, "supports_colour_temp", False)),
            },
        }
        self._cached_state = state
        self._cached_at = time.monotonic()
        return state

    async def handle(self, req):
        op = req.get("op")
        if op not in ("state", "set", "scene", "effect"):
            raise ValueError("unknown op %r" % (op,))
        async with self.lock:
            try:
                await self._ensure_connected()
                if op == "state":
                    return await self._state(force=False)

                if op == "set":
                    applied = await self._apply(req)
                elif op == "scene":
                    applied = await self._apply_scene(req.get("name"))
                else:
                    applied = await self._apply_effect(req)

                # Report the write's own values rather than polling the bulb
                # back. A poll after every write doubles the BLE traffic of a
                # long run of them -- a 4-minute fade writes every couple of
                # seconds -- and that extra traffic is what starts producing
                # ReadWriteError partway through. A write that returned
                # without raising did take effect, so its values are what the
                # bulb now holds.
                return self._merge_state(applied)
            except Exception:
                self._cached_state = None
                raise

    def _merge_state(self, applied):
        state = dict(self._cached_state or {})
        state.update(applied)
        state["reachable"] = True
        self._cached_state = state
        self._cached_at = time.monotonic()
        return state

    async def _apply(self, spec):
        # Power first: setting brightness or colour on a bulb that is off
        # turns it on as a side effect, which makes {"on": false} racy if it
        # is applied last.
        applied = {}
        if "power" in spec:
            await self.light.set_power(bool(spec["power"]))
            applied["power"] = bool(spec["power"])
        if "brightness" in spec:
            await self.light.set_brightness(int(spec["brightness"]))
            applied["brightness"] = int(spec["brightness"])
        if "temp" in spec:
            await self.light.set_colour_temp(int(spec["temp"]))
            # Setting either colour mode invalidates the other, exactly as
            # the bulb reports it: a sentinel, surfaced here as None.
            applied["temp"] = int(spec["temp"])
            applied["xy"] = None
        if spec.get("xy"):
            await self.light.set_colour_xy(float(spec["xy"][0]), float(spec["xy"][1]))
            applied["xy"] = [float(spec["xy"][0]), float(spec["xy"][1])]
            applied["temp"] = None
        return applied

    async def _apply_effect(self, req):
        effect = EffectType[str(req.get("name", "none")).upper()]
        speed = max(0, min(255, int(req.get("speed", 128))))
        if req.get("kind") == "temperature":
            await self.light.set_temperature_effect(
                effect, int(req["temp"]), speed
            )
        else:
            await self.light.set_colour_effect(effect, speed)
        return {"effect": {"name": effect.name, "speed": speed}}

    async def _apply_scene(self, name):
        scenes = hue_ble.load_scenes()
        scene = scenes.get(str(name))
        if not isinstance(scene, dict):
            raise ValueError("no such scene: %s" % name)
        spec = {}
        if "power" in scene:
            spec["power"] = bool(scene["power"])
        if "brightness" in scene:
            spec["brightness"] = int(scene["brightness"])
        if "temp" in scene:
            spec["temp"] = int(scene["temp"])
        colour = scene.get("colour") or scene.get("color")
        if colour in COLOURS:
            spec["xy"] = list(COLOURS[colour])
        # A scene that sets anything other than power implies the light
        # should be on; the file format has no explicit "on" for those.
        if spec and "power" not in spec:
            spec["power"] = True
        return await self._apply(spec)


async def serve(bulb, path):
    async def on_client(reader, writer):
        try:
            line = await reader.readline()
            if not line:
                return
            req = json.loads(line)
            reply = {"ok": True, "state": await bulb.handle(req)}
        except Exception as e:
            # Errors go back as data, not as a dropped connection: the client
            # turns them into a non-zero exit, which is what the panel reads.
            reply = {"ok": False, "error": "%s: %s" % (type(e).__name__, e)}
        try:
            writer.write((json.dumps(reply) + "\n").encode())
            await writer.drain()
        except Exception:
            pass
        finally:
            writer.close()

    if os.path.exists(path):
        os.unlink(path)
    server = await asyncio.start_unix_server(on_client, path=path)
    # The socket lives in XDG_RUNTIME_DIR (already 0700), but the bulb is
    # controllable by anyone who can write here -- be explicit about it.
    os.chmod(path, 0o600)
    async with server:
        await server.serve_forever()


def _load_address():
    with open(CREDS_FILE, "rb") as f:
        creds = json.loads(f.read(64 * 1024))
    cfg = hue_ble.ble_config(creds)
    if not cfg:
        raise SystemExit("hue.json has no valid ble transport configured")
    return cfg["address"]


def main():
    address = _load_address()
    bulb = Bulb(address)
    try:
        asyncio.run(serve(bulb, hue_ble.socket_path()))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
