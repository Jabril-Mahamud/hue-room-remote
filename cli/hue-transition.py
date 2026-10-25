#!/home/linuxbrew/.linuxbrew/bin/python3.14
"""Hue BLE light - warm-to-work transition with gradual fade."""

import asyncio
import sys
import time
from pathlib import Path
from bleak import BleakScanner
from HueBLE import HueBleLight

sys.path.insert(0, str(Path(__file__).parent))
import importlib.util

_spec = importlib.util.spec_from_file_location(
    "huectl", Path(__file__).with_name("hue-control.py")
)
huectl = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(huectl)

ADDR = huectl.ADDR

# Default presets
WARM = {"temp": 400, "brightness": 180}   # warm white, cozy
WORK = {"temp": 250, "brightness": 254}    # cool white, focused

async def set_light(light, temp=None, brightness=None):
    """Set light state. Only sends commands for values that changed."""
    if brightness is not None:
        await light.set_brightness(brightness)
    if temp is not None:
        await light.set_colour_temp(temp)

async def fade(light, from_temp, to_temp, from_brightness, to_brightness, duration_sec):
    """Gradually transition from one state to another over duration_sec seconds."""
    steps = max(int(duration_sec / 2), 1)  # update every ~2 seconds
    for i in range(steps + 1):
        t = i / steps
        current_temp = int(from_temp + (to_temp - from_temp) * t)
        current_brightness = int(from_brightness + (to_brightness - from_brightness) * t)
        await set_light(light, temp=current_temp, brightness=current_brightness)
        if i < steps:
            await asyncio.sleep(duration_sec / steps)

async def main():
    if len(sys.argv) < 2:
        print("Usage: hue-transition.py <warm|work|fade|schedule> [options]")
        print()
        print("  warm              - Set warm light immediately")
        print("  work              - Set work light immediately")
        print("  fade <minutes>    - Gradually fade from warm to work over N minutes")
        print("  schedule <minutes> - Warm now, then fade to work after N minutes")
        print()
        print("  Options (for warm/work/fade/schedule):")
        print("    --warm-temp <153-500>     Warm colour temp (default: 400)")
        print("    --work-temp <153-500>     Work colour temp (default: 250)")
        print("    --warm-brightness <0-254> Warm brightness (default: 180)")
        print("    --work-brightness <0-254> Work brightness (default: 254)")
        print("    --fade-minutes <N>        Fade duration in minutes (default: 5)")
        return

    cmd = sys.argv[1].lower()

    # Parse optional overrides
    def get_arg(name, default):
        try:
            idx = sys.argv.index(name)
            return int(sys.argv[idx + 1])
        except (ValueError, IndexError):
            return default

    warm_temp = get_arg("--warm-temp", WARM["temp"])
    work_temp = get_arg("--work-temp", WORK["temp"])
    warm_brightness = get_arg("--warm-brightness", WARM["brightness"])
    work_brightness = get_arg("--work-brightness", WORK["brightness"])
    fade_minutes = get_arg("--fade-minutes", 5)

    device = await BleakScanner.find_device_by_address(ADDR, timeout=5.0)
    light = HueBleLight(device)
    await light.connect()

    try:
        if cmd == "warm":
            await light.set_power(True)
            await set_light(light, temp=warm_temp, brightness=warm_brightness)
            print(f"Warm light set: temp={warm_temp}, brightness={warm_brightness}")

        elif cmd == "work":
            await light.set_power(True)
            await set_light(light, temp=work_temp, brightness=work_brightness)
            print(f"Work light set: temp={work_temp}, brightness={work_brightness}")

        elif cmd == "fade":
            minutes = int(sys.argv[2]) if len(sys.argv) > 2 else fade_minutes
            await light.set_power(True)
            # Read current state as starting point
            await light.poll_state()
            from_temp = light.colour_temp if light.colour_temp != 65535 else warm_temp
            from_brightness = light.brightness
            print(f"Fading from temp={from_temp}, brightness={from_brightness} "
                  f"to temp={work_temp}, brightness={work_brightness} "
                  f"over {minutes} minutes...")
            await fade(light, from_temp, work_temp, from_brightness, work_brightness, minutes * 60)
            print("Fade complete.")

        elif cmd == "schedule":
            delay_minutes = int(sys.argv[2]) if len(sys.argv) > 2 else 30
            # Phase 1: warm now
            await light.set_power(True)
            await set_light(light, temp=warm_temp, brightness=warm_brightness)
            print(f"Phase 1: Warm light set (temp={warm_temp}, brightness={warm_brightness})")
            print(f"Waiting {delay_minutes} minutes before transitioning to work light...")
            print(f"(Script will run in background - do not close this terminal)")

            # Wait
            await asyncio.sleep(delay_minutes * 60)

            # Phase 2: fade to work
            print(f"Phase 2: Fading to work light over {fade_minutes} minutes...")
            await fade(light, warm_temp, work_temp, warm_brightness, work_brightness, fade_minutes * 60)
            print("Transition complete - work light active.")

        else:
            print(f"Unknown command: {cmd}")
            return

    finally:
        await light.disconnect()

asyncio.run(main())
