#!/home/linuxbrew/.linuxbrew/bin/python3.14
"""Hue BLE light control - full-featured CLI for the desk/bedroom bulb."""

import json
import os
import time
import asyncio
import sys
from pathlib import Path
from bleak import BleakScanner
from HueBLE import HueBleLight, EffectType

# This firmware can report effect ids HueBLE's enum does not define (0x0D is one).
# EffectType(raw) then raises inside poll_effects and the light becomes unreachable
# until something writes a known effect back. Treat unknown ids as "no effect".
EffectType._missing_ = classmethod(lambda cls, value: cls.NONE)

# The bulb's address changes when it is re-paired, so read the one ble_setup.sh
# last recorded instead of pinning it here.
_CREDS = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state") / "omarchy/settings/hue.json"
ADDR = json.loads(_CREDS.read_text())["ble"]["address"]
TEMP_MIN, TEMP_MAX = 153, 500
DEFAULT_TEMP = 370  # fallback when the bulb reports a temp outside the valid range

sys.path.insert(0, str(Path(__file__).parent))
import hue_daemon_light


async def acquire_light():
    """Return a connected light, preferring hue-ble-daemon if it is running.

    The bulb accepts one BLE connection at a time, so while the daemon holds
    it a direct connect here can only fail. Going through the daemon also
    skips the 5-14 s scan-and-connect these scripts otherwise pay per run.
    """
    if hue_daemon_light.daemon_running():
        light = hue_daemon_light.DaemonLight()
        await light.connect()
        return light
    device = await BleakScanner.find_device_by_address(ADDR, timeout=5.0)
    if device is None:
        raise RuntimeError(f"Light not found at {ADDR}")
    light = HueBleLight(device)
    await light.connect()
    return light

COLORS = {
    "red": (0.67, 0.30),
    "orange": (0.58, 0.38),
    "yellow": (0.45, 0.48),
    "green": (0.21, 0.65),
    "blue": (0.15, 0.06),
    "purple": (0.28, 0.12),
    "pink": (0.40, 0.22),
    "white": (0.32, 0.34),
    "warm": (0.45, 0.40),
    "cool": (0.30, 0.32),
    "amber": (0.55, 0.42),
    # Keep in step with COLOURS in the plugin's hue_ble_daemon.py.
    "rose": (0.50, 0.30),
    "coral": (0.60, 0.34),
    "peach": (0.50, 0.40),
    "teal": (0.17, 0.35),
    "lavender": (0.30, 0.20),
    "magenta": (0.38, 0.16),
}

EFFECTS = {
    "none": EffectType.NONE,
    "candle": EffectType.CANDLE,
    "fireplace": EffectType.FIREPLACE,
    "prism": EffectType.PRISM,
    "sparkle": EffectType.SPARKLE,
    "opal": EffectType.OPAL,
    "glisten": EffectType.GLISTEN,
    "underwater": EffectType.UNDERWATER,
    "cosmos": EffectType.COSMOS,
    "sunbeam": EffectType.SUNBEAM,
    "enchant": EffectType.ENCHANT,
}


def valid_xy(xy):
    """True if xy is a real chromaticity, not the (1.0, 1.0) sentinel.

    In temperature mode the bulb reports 0xFFFF/0xFFFF for both coordinates.
    Packing that back into a write makes the bulb reject the whole command.
    """
    if not xy:
        return False
    x, y = xy
    return x > 0 and y > 0 and x + y <= 1.0


async def set_effect(light, name, speed=128, brightness=None, xy=None, temp=None):
    """Set effect `name` on the light, or clear a running one.

    "none"/"off" cannot be written through the effects characteristic: the bulb
    accepts effect=0 and then ignores it, and HueBLE omits the effect field
    entirely for EffectType.NONE, so the bulb is never told to stop. A write to
    the temperature characteristic does stop it, at the cost of dropping back to
    white -- the same trick hue-tui.py's cycle-to-none uses.

    xy, temp and brightness override what the bulb currently holds, so a scene can
    set colour, brightness and effect in one write. Passing temp forces the
    temperature characteristic even if the bulb is in colour mode.
    """
    name = name.lower()
    if name not in EFFECTS and name != "off":
        print(f"Unknown effect: {name}. Options: {', '.join(EFFECTS.keys())}")
        return False

    await light.poll_state()
    bri = int(brightness if brightness is not None else (light.brightness or 128))
    target_temp = temp if temp is not None else light.colour_temp
    if not target_temp or not TEMP_MIN <= target_temp <= TEMP_MAX:
        target_temp = DEFAULT_TEMP

    if name in ("none", "off"):
        await light.set_colour_temp(int(target_temp))
        return True

    target_xy = xy if valid_xy(xy) else None
    if target_xy is None and temp is None and valid_xy(light.colour_xy):
        target_xy = light.colour_xy

    if target_xy and light.supports_colour_xy:
        await light.set_colour_effect(
            float(target_xy[0]), float(target_xy[1]), bri, EFFECTS[name], int(speed)
        )
    else:
        await light.set_temperature_effect(int(target_temp), bri, EFFECTS[name], int(speed))
    return True


def print_usage():
    print("Usage: hue-control.py <command> [args...]")
    print()
    print("Basic:")
    print("  on                          Turn light on")
    print("  off                         Turn light off")
    print("  toggle                      Toggle power")
    print("  state                       Show current state")
    print("  fade on <target_bri> <seconds>  Fade brightness up over N seconds")
    print("  fade off <seconds>              Fade brightness down to 0 over N seconds")
    print()
    print("Settings:")
    print("  brightness <0-254>          Set brightness")
    print("  temp <153-500>              Set colour temperature (153=cool, 500=warm)")
    print("  color <name>                Set solid colour")
    print("  color <x> <y>              Set CIE XY colour")
    print()
    print("Effects:")
    print("  effect <name> [speed]       Set effect (speed 0-255, default 128)")
    print("  effect off                  Turn off effect (back to solid colour)")
    print()
    print("Scenes (~/.config/hue-scenes.json, via hue-fade.py):")
    print("  a scene may carry \"effect\" and \"speed\", or be {\"shuffle\": true}")
    print()
    print("Combined (one command):")
    print("  scene <color> <effect> [brightness] [speed]")
    print("  scene temp <mireds> <effect> [brightness] [speed]")
    print()
    print("Flags:")
    print("  --on                        Turn on before setting (for temp/color/brightness)")
    print("  --retry                     Retry up to 3 times on BLE errors")
    print()
    print("Available colours:", ", ".join(COLORS.keys()))
    print("Available effects:", ", ".join(EFFECTS.keys()))
    print()
    print("Examples:")
    print("  hue-control.py scene amber candle 80")
    print("  hue-control.py scene red fireplace 60 200")
    print("  hue-control.py scene temp 400 candle 100")
    print("  hue-control.py effect cosmos 150")
    print("  hue-control.py temp 250 --retry")
    print("  hue-control.py fade on 200 60")
    print("  hue-control.py fade off 30")


async def run_command(light, cmd, auto_on):
    """Execute a single command. Returns True on success, raises on failure."""
    if cmd == "on":
        await light.set_power(True)

    elif cmd == "off":
        await light.set_power(False)

    elif cmd == "toggle":
        await light.poll_state()
        await light.set_power(not light.power_state)

    elif cmd == "state":
        await light.poll_state()
        print(f"Power: {light.power_state}")
        print(f"Brightness: {light.brightness}")
        print(f"Colour temp: {light.colour_temp}")
        print(f"XY: {light.colour_xy}")
        if light.supports_effects:
            effect = light.effect
            if effect:
                effect_name = {v: k for k, v in EFFECTS.items()}.get(effect[0], str(effect[0]))
                print(f"Effect: {effect_name} (speed: {effect[1]})")
            else:
                print("Effect: none")
        return True

    elif cmd == "fade":
        if len(sys.argv) < 3:
            print("Usage: hue-control.py fade on <target_bri> <seconds>")
            print("       hue-control.py fade off <seconds>")
            return False

        sub = sys.argv[2].lower()
        if sub == "on":
            if len(sys.argv) < 5:
                print("Usage: hue-control.py fade on <target_bri> <seconds>")
                return False
            target_bri = int(sys.argv[3])
            duration = int(sys.argv[4])
            await light.poll_state()
            start_bri = light.brightness or 1
            await light.set_power(True)
            steps = max(duration * 2, 10)
            step_delay = duration / steps
            for i in range(1, steps + 1):
                bri = int(start_bri + (target_bri - start_bri) * (i / steps))
                bri = max(1, min(254, bri))
                await light.set_brightness(bri)
                time.sleep(step_delay)
            print(f"OK - Faded up to brightness {target_bri} over {duration}s")

        elif sub == "off":
            if len(sys.argv) < 4:
                print("Usage: hue-control.py fade off <seconds>")
                return False
            duration = int(sys.argv[3])
            await light.poll_state()
            start_bri = light.brightness or 128
            steps = max(duration * 2, 10)
            step_delay = duration / steps
            for i in range(1, steps + 1):
                bri = int(start_bri * (1 - i / steps))
                bri = max(1, min(254, bri))
                await light.set_brightness(bri)
                time.sleep(step_delay)
            await light.set_power(False)
            print(f"OK - Faded down to off over {duration}s")
        else:
            print(f"Unknown fade subcommand: {sub}")
            return False

    elif cmd == "brightness":
        if auto_on:
            await light.set_power(True)
        val = int(sys.argv[2])
        await light.set_brightness(val)

    elif cmd == "temp":
        if auto_on:
            await light.set_power(True)
        val = int(sys.argv[2])
        await light.set_colour_temp(val)

    elif cmd == "color":
        if auto_on:
            await light.set_power(True)
        if len(sys.argv) == 3:
            name = sys.argv[2].lower()
            if name not in COLORS:
                print(f"Unknown colour: {name}. Options: {', '.join(COLORS.keys())}")
                return False
            x, y = COLORS[name]
            await light.set_colour_xy(x, y)
        elif len(sys.argv) == 4:
            x, y = float(sys.argv[2]), float(sys.argv[3])
            await light.set_colour_xy(x, y)
        else:
            print("Usage: hue-control.py color <name> | color <x> <y>")
            return False

    elif cmd == "effect":
        if len(sys.argv) < 3:
            print("Usage: hue-control.py effect <name> [speed]")
            print("       hue-control.py effect off")
            return False

        speed = int(sys.argv[3]) if len(sys.argv) >= 4 else 128
        if not await set_effect(light, sys.argv[2], speed):
            return False

    elif cmd == "scene":
        if len(sys.argv) < 4:
            print("Usage: hue-control.py scene <color> <effect> [brightness] [speed]")
            print("       hue-control.py scene temp <mireds> <effect> [brightness] [speed]")
            return False

        if auto_on:
            await light.set_power(True)

        if sys.argv[2].lower() == "temp":
            if len(sys.argv) < 5:
                print("Usage: hue-control.py scene temp <mireds> <effect> [brightness] [speed]")
                return False
            ct = int(sys.argv[3])
            bri = int(sys.argv[5]) if len(sys.argv) >= 6 else 128
            speed = int(sys.argv[6]) if len(sys.argv) >= 7 else 128
            if not await set_effect(light, sys.argv[4], speed, brightness=bri, temp=ct):
                return False
        else:
            color_name = sys.argv[2].lower()
            if color_name not in COLORS:
                print(f"Unknown colour: {color_name}")
                return False
            bri = int(sys.argv[4]) if len(sys.argv) >= 5 else 128
            speed = int(sys.argv[5]) if len(sys.argv) >= 6 else 128
            if not await set_effect(light, sys.argv[3], speed,
                                    brightness=bri, xy=COLORS[color_name]):
                return False

    else:
        print(f"Unknown command: {cmd}")
        print_usage()
        return False

    # Verify the write actually took effect
    await light.poll_state()
    effect_str = ""
    if light.supports_effects and light.effect and light.effect[0] != EffectType.NONE:
        effect_name = {v: k for k, v in EFFECTS.items()}.get(light.effect[0], str(light.effect[0]))
        effect_str = f", Effect: {effect_name} (speed: {light.effect[1]})"
    print(f"OK - Power: {light.power_state}, Brightness: {light.brightness}{effect_str}")
    return True


async def main():
    if len(sys.argv) < 2:
        print_usage()
        return

    cmd = sys.argv[1].lower()

    # Parse flags
    auto_on = "--on" in sys.argv
    if auto_on:
        sys.argv.remove("--on")

    retries = 3 if "--retry" in sys.argv else 1
    if "--retry" in sys.argv:
        sys.argv.remove("--retry")

    last_error = None
    for attempt in range(retries):
        light = None
        try:
            light = await acquire_light()

            success = await run_command(light, cmd, auto_on)
            if not success:
                sys.exit(1)
            return  # success

        except Exception as e:
            last_error = e
            if attempt < retries - 1:
                print(f"Retry {attempt + 1}/{retries - 1}: {e}", file=sys.stderr)
                await asyncio.sleep(1)
            else:
                print(f"ERROR (after {retries} attempt(s)): {e}", file=sys.stderr)
                sys.exit(1)
        finally:
            if light:
                await light.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
