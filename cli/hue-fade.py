#!/home/linuxbrew/.linuxbrew/bin/python3.14
"""Fade the Hue bulb from wherever it is now to a named scene.

Scenes live in ~/.config/hue-scenes.json and are plain editable JSON. A scene
sets brightness plus either a colour temperature or an xy colour, or turns the
light off. Fading to "off" dims down first and cuts power at the end, so it
works as a sleep fade.

A scene can also carry an `effect` (plus optional `speed`), or be `{"shuffle":
true}`, which resolves to a random one of the others.

Usage:
  hue-fade.py list
  hue-fade.py <scene> [minutes]      minutes 0 (default) = jump straight there
  hue-fade.py save <name>            capture the light's current state as a scene
"""

import asyncio
import json
import random
import sys
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
COLORS = huectl.COLORS

SCENES_FILE = Path.home() / ".config" / "hue-scenes.json"
STEP_SECONDS = 2  # how often the fade pushes a new value to the bulb
TEMP_MIN, TEMP_MAX = 153, 500
BRI_MAX = 254

DEFAULT_SCENES = {
    "wake": {"temp": 320, "brightness": 200},
    "day": {"temp": 250, "brightness": 254},
    "evening": {"temp": 430, "brightness": 110},
    "night": {"temp": 500, "brightness": 12},
    "off": {"power": False},
    "alert": {"flash": 5, "colour": "red", "brightness": 254},
}

# A flash blinks by cutting power rather than dropping brightness to its floor:
# the bulb ramps brightness smoothly, which reads as a pulse, and power toggles
# read as a sharp blink. Each BLE write already costs ~0.25s, so that is the real
# floor on blink rate and this interval is only the extra hold on top — measured
# ~0.7s per on/off cycle at 0.12. Override per scene with "interval".
FLASH_INTERVAL = 0.12


def load_scenes():
    if not SCENES_FILE.exists():
        SCENES_FILE.parent.mkdir(parents=True, exist_ok=True)
        SCENES_FILE.write_text(json.dumps(DEFAULT_SCENES, indent=2) + "\n")
        return dict(DEFAULT_SCENES)
    return json.loads(SCENES_FILE.read_text())


def save_scenes(scenes):
    SCENES_FILE.parent.mkdir(parents=True, exist_ok=True)
    SCENES_FILE.write_text(json.dumps(scenes, indent=2) + "\n")


def clamp(value, lo, hi):
    return max(lo, min(hi, value))


def valid_xy(xy):
    """Reject the (1.0, 1.0) sentinel the bulb reports in temperature mode."""
    if not xy:
        return False
    x, y = xy
    return x > 0 and y > 0 and x + y <= 1.0


def is_flash(scene):
    return bool(scene.get("flash"))


def describe_scene(scene):
    if scene.get("shuffle"):
        return "random scene"
    if scene.get("power") is False:
        return "off"
    if is_flash(scene):
        colour = scene.get("colour") or ("temp %s" % scene["temp"] if "temp" in scene else "")
        return f"flash {scene['flash']}× {colour}".strip()
    bits = []
    if "temp" in scene:
        bits.append(f"temp {scene['temp']}")
    if "colour" in scene:
        bits.append(scene["colour"])
    if "xy" in scene:
        bits.append("xy %.2f,%.2f" % tuple(scene["xy"]))
    if "brightness" in scene:
        bits.append(f"bri {round(scene['brightness'] / BRI_MAX * 100)}%")
    if scene.get("effect"):
        bits.append("+" + scene["effect"])
    return "  ".join(bits) or "?"


def scene_xy(scene):
    """Target xy for a scene, resolving a colour name if that is how it is written."""
    if "xy" in scene:
        return tuple(scene["xy"])
    if "colour" in scene:
        return COLORS.get(scene["colour"])
    return None


def resolve_scene(scenes, name):
    """(name, scene) for `name`, following a shuffle entry to a random pick.

    Power-off, flash and shuffle scenes are never picked: a surprise that turns
    the light off, fires the alert blink or lands back on itself is not one.
    """
    scene = scenes.get(name)
    if isinstance(scene, dict) and scene.get("shuffle"):
        pool = sorted(
            n for n, s in scenes.items()
            if isinstance(s, dict) and s.get("power") is not False
            and not s.get("flash") and not s.get("shuffle")
        )
        if not pool:
            return name, None
        name = random.choice(pool)
        scene = scenes[name]
    return name, scene if isinstance(scene, dict) else None


def frame(start, target, t):
    """State at fraction t (0..1) of a fade. Pure, so it can be tested offline.

    Interpolates brightness always, plus whichever colour axis both ends share.
    If the two ends are in different modes (xy vs temperature) there is no
    meaningful path between them, so the colour is set once at the start and
    only brightness moves.
    """
    out = {}
    if start.get("brightness") is not None and target.get("brightness") is not None:
        value = start["brightness"] + (target["brightness"] - start["brightness"]) * t
        out["brightness"] = int(clamp(round(value), 1, BRI_MAX))

    s_xy, t_xy = start.get("xy"), target.get("xy")
    s_temp, t_temp = start.get("temp"), target.get("temp")
    if t_xy and s_xy:
        out["xy"] = tuple(s + (e - s) * t for s, e in zip(s_xy, t_xy))
    elif t_xy:
        out["xy"] = t_xy  # different modes: snap colour, fade brightness only
    elif t_temp and s_temp:
        value = s_temp + (t_temp - s_temp) * t
        out["temp"] = int(clamp(round(value), TEMP_MIN, TEMP_MAX))
    elif t_temp:
        out["temp"] = t_temp
    return out


def read_state(light):
    state = {"brightness": light.brightness or 1}
    if valid_xy(light.colour_xy):
        state["xy"] = tuple(light.colour_xy)
    elif light.colour_temp and TEMP_MIN <= light.colour_temp <= TEMP_MAX:
        state["temp"] = light.colour_temp
    # Only reported when one is actually running, so "effect" in state answers
    # "is an effect in the way of this fade?" as well as feeding save.
    effect = getattr(light, "effect", None)
    if effect and effect[0].name != "NONE":
        state["effect"] = effect[0].name.lower()
    return state


def target_state(scene, start):
    """Turn a scene into concrete start-comparable values."""
    if scene.get("power") is False:
        # Fading off means riding brightness to the floor, then cutting power.
        return {"brightness": 1, **({"xy": start["xy"]} if "xy" in start
                                    else {"temp": start["temp"]} if "temp" in start else {})}
    target = {"brightness": scene.get("brightness", BRI_MAX)}
    xy = scene_xy(scene)
    if xy:
        target["xy"] = xy
    elif "temp" in scene:
        target["temp"] = scene["temp"]
    return target


async def push(light, values):
    if "brightness" in values:
        await light.set_brightness(values["brightness"])
    if "xy" in values:
        await light.set_colour_xy(*values["xy"])
    elif "temp" in values:
        await light.set_colour_temp(values["temp"])


async def run_flash(light, scene):
    """Blink N times, then put the light back exactly as it was.

    A flash is an interruption, not a state change, so the previous colour,
    brightness and power state are all restored at the end — including leaving
    the light off if it was off.
    """
    await light.poll_state()
    before = read_state(light)
    was_on = light.power_state

    peak = {"brightness": scene.get("brightness", BRI_MAX)}
    xy = scene_xy(scene)
    if xy:
        peak["xy"] = xy
    elif "temp" in scene:
        peak["temp"] = scene["temp"]

    interval = scene.get("interval", FLASH_INTERVAL)
    for i in range(int(scene["flash"])):
        await light.set_power(True)
        if i == 0:
            await push(light, peak)  # colour only needs setting once
        await asyncio.sleep(interval)
        await light.set_power(False)
        await asyncio.sleep(interval)

    await light.set_power(True)
    await push(light, before)
    if not was_on:
        await light.set_power(False)


async def run_fade(light, scene, minutes):
    if is_flash(scene):
        await run_flash(light, scene)
        return

    await light.poll_state()
    start = read_state(light)
    target = target_state(scene, start)
    going_off = scene.get("power") is False

    if "effect" in start and not scene.get("effect"):
        # Only a temperature write stops a running effect. Left going, it keeps
        # overwriting the colour this fade is trying to set.
        await light.set_colour_temp(int(start.get("temp") or huectl.DEFAULT_TEMP))
        start.pop("effect")

    if not going_off and not light.power_state:
        # Coming up from off: start dark so the fade is a real ramp, not a jump.
        await light.set_brightness(1)
        await light.set_power(True)
        start["brightness"] = 1

    seconds = minutes * 60
    steps = max(int(seconds / STEP_SECONDS), 1) if seconds else 1
    for i in range(1, steps + 1):
        await push(light, frame(start, target, i / steps))
        if i < steps:
            await asyncio.sleep(seconds / steps)

    if going_off:
        await light.set_power(False)
    elif scene.get("effect"):
        # At the end, not per frame: an effect write costs the same as a colour
        # write and the effect would fight the ramp the whole way up.
        await huectl.set_effect(light, scene["effect"], scene.get("speed", 128),
                                brightness=target.get("brightness"),
                                xy=target.get("xy"), temp=target.get("temp"))


def scene_from_state(state):
    """A storable scene from a light's current readings."""
    scene = {"brightness": state["brightness"]}
    if "xy" in state:
        scene["xy"] = [round(v, 4) for v in state["xy"]]
    elif "temp" in state:
        scene["temp"] = state["temp"]
    if "effect" in state:
        scene["effect"] = state["effect"]
    return scene


def store_scene(name, scene):
    scenes = load_scenes()
    scenes[name] = scene
    save_scenes(scenes)
    return scene


async def capture(light, name):
    await light.poll_state()
    return store_scene(name, scene_from_state(read_state(light)))


async def with_light(action):
    light = await huectl.acquire_light()
    try:
        return await action(light)
    finally:
        await light.disconnect()


def demo():
    start = {"temp": 400, "brightness": 10}
    target = {"temp": 250, "brightness": 254}
    assert frame(start, target, 0) == {"temp": 400, "brightness": 10}
    assert frame(start, target, 1) == {"temp": 250, "brightness": 254}
    mid = frame(start, target, 0.5)
    assert mid["temp"] == 325 and mid["brightness"] == 132

    # colour to colour interpolates both coordinates
    cxy = frame({"xy": (0.7, 0.3), "brightness": 100},
                {"xy": (0.2, 0.6), "brightness": 100}, 0.5)
    assert abs(cxy["xy"][0] - 0.45) < 1e-9 and abs(cxy["xy"][1] - 0.45) < 1e-9

    # crossing modes cannot interpolate colour, so it snaps and brightness fades
    crossed = frame({"temp": 400, "brightness": 10},
                    {"xy": (0.5, 0.4), "brightness": 200}, 0.5)
    assert crossed["xy"] == (0.5, 0.4) and crossed["brightness"] == 105

    # brightness never reaches 0 mid-fade: the bulb treats 0 as invalid
    assert frame({"brightness": 1}, {"brightness": 1}, 0)["brightness"] == 1
    assert frame({"temp": 300, "brightness": 254},
                 {"temp": 300, "brightness": 1}, 1)["brightness"] == 1

    # fading off keeps the current colour and rides brightness to the floor
    off = target_state({"power": False}, {"temp": 400, "brightness": 200})
    assert off == {"brightness": 1, "temp": 400}
    off_xy = target_state({"power": False}, {"xy": (0.5, 0.4), "brightness": 200})
    assert off_xy == {"brightness": 1, "xy": (0.5, 0.4)}

    # a named colour resolves through the shared COLORS table
    assert scene_xy({"colour": "amber"}) == COLORS["amber"]
    assert scene_xy({"xy": [0.3, 0.3]}) == (0.3, 0.3)
    assert scene_xy({"temp": 300}) is None
    assert target_state({"colour": "red", "brightness": 100}, {})["xy"] == COLORS["red"]

    # capturing the light's state round-trips back through the fader
    assert scene_from_state({"brightness": 200, "temp": 300}) == \
        {"brightness": 200, "temp": 300}
    captured = scene_from_state({"brightness": 200, "xy": (0.123456, 0.4)})
    assert captured == {"brightness": 200, "xy": [0.1235, 0.4]}  # rounded for the file
    assert scene_xy(captured) == (0.1235, 0.4)

    assert not valid_xy((1.0, 1.0)) and valid_xy((0.3, 0.3))
    assert describe_scene({"power": False}) == "off"
    assert describe_scene({"shuffle": True}) == "random scene"
    assert "temp 250" in describe_scene(DEFAULT_SCENES["day"])

    # a flash is its own scene kind, not a fade target
    assert is_flash(DEFAULT_SCENES["alert"])
    assert not is_flash(DEFAULT_SCENES["day"])
    assert not is_flash({"flash": 0})  # zero blinks is not a flash
    assert describe_scene(DEFAULT_SCENES["alert"]) == "flash 5× red"
    assert describe_scene({"flash": 3, "temp": 200}) == "flash 3× temp 200"
    # a flash scene still resolves a colour the same way a fade scene does
    assert scene_xy(DEFAULT_SCENES["alert"]) == COLORS["red"]

    # an effect rides along with a scene, through describe and through save
    assert describe_scene({"colour": "orange", "brightness": 110,
                           "effect": "fireplace"}).endswith("+fireplace")
    assert scene_from_state({"brightness": 90, "temp": 450, "effect": "candle"}) == \
        {"brightness": 90, "temp": 450, "effect": "candle"}
    assert "effect" not in scene_from_state({"brightness": 90, "temp": 450})

    # shuffle resolves to a real mood scene, never to off, a flash or itself
    pool_scenes = {
        "surprise": {"shuffle": True}, "off": {"power": False},
        "alert": {"flash": 5}, "day": {"temp": 250}, "cosmos": {"effect": "cosmos"},
    }
    for _ in range(50):
        picked, scene = resolve_scene(pool_scenes, "surprise")
        assert picked in ("day", "cosmos"), picked
        assert scene is pool_scenes[picked]
    assert resolve_scene(pool_scenes, "day") == ("day", pool_scenes["day"])
    assert resolve_scene(pool_scenes, "nope") == ("nope", None)
    # nothing to pick from is a miss, not a crash
    assert resolve_scene({"surprise": {"shuffle": True}}, "surprise") == ("surprise", None)
    print("selftest ok")


def main():
    args = sys.argv[1:]
    if not args or args[0] == "--selftest":
        demo() if args else print(__doc__)
        return

    if args[0] == "list":
        for name, scene in load_scenes().items():
            print(f"  {name:<10} {describe_scene(scene)}")
        return

    if args[0] == "save":
        name = args[1]
        scene = asyncio.run(with_light(lambda light: capture(light, name)))
        print(f"saved {name}: {describe_scene(scene)}")
        return

    name = args[0]
    minutes = float(args[1]) if len(args) > 1 else 0
    scenes = load_scenes()
    name, scene = resolve_scene(scenes, name)
    if scene is None:
        print(f"unknown scene: {name}. Have: {', '.join(scenes)}")
        sys.exit(1)
    asyncio.run(with_light(lambda light: run_fade(light, scene, minutes)))
    print(f"{name} ({describe_scene(scene)}) over {minutes}min")


if __name__ == "__main__":
    main()
