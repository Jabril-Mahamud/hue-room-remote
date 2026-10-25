#!/home/linuxbrew/.linuxbrew/bin/python3.14
"""Sanity check for the daemon-backed light proxy. Needs hue-ble-daemon up.

Not a unit test -- it talks to the real daemon, because the thing worth
checking is that DaemonLight still presents everything hue-control.py,
hue-fade.py and hue-tui.py read off a real HueBleLight. Read-only: it never
changes the bulb.

    ./test_hue_daemon_light.py
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import hue_daemon_light as hdl

# Every member the scripts in this directory touch, per
# `grep -ohE "light\.[a-z_]+" hue-*.py`.
READ_MEMBERS = (
    "power_state", "brightness", "colour_temp", "colour_xy", "effect",
    "connected", "supports_effects", "supports_colour_xy",
    "supports_colour_temp",
)
WRITE_METHODS = (
    "set_power", "set_brightness", "set_colour_temp", "set_colour_xy",
    "set_colour_effect", "set_temperature_effect", "connect", "disconnect",
    "poll_state", "poll_brightness", "poll_effects",
)


async def main():
    assert hdl.daemon_running(), (
        "daemon socket missing -- systemctl --user start hue-ble-daemon"
    )

    light = hdl.DaemonLight()
    await light.connect()

    for name in READ_MEMBERS:
        getattr(light, name)
    for name in WRITE_METHODS:
        assert callable(getattr(light, name)), name

    # effect must stay a (EffectType, speed) tuple: callers index [0]/[1] and
    # compare against EffectType.NONE.
    effect = light.effect
    assert effect is None or (isinstance(effect, tuple) and len(effect) == 2), effect

    # hue-tui.py stashes its own UI intent on the light object.
    assert getattr(light, "ui_bri_pct", None) is None
    light.ui_bri_pct = 42
    assert light.ui_bri_pct == 42

    await light.poll_state()
    assert light.brightness is not None
    await light.disconnect()

    print("ok - power=%s bri=%s temp=%s effect=%s" % (
        light.power_state, light.brightness, light.colour_temp, effect))


if __name__ == "__main__":
    asyncio.run(main())
