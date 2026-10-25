#!/home/linuxbrew/.linuxbrew/bin/python3.14
"""Interactive TUI for the Hue BLE bulb.

Holds one BLE connection open for the whole session, so a keypress costs
milliseconds instead of the ~7s scan+connect+disconnect that a fresh
hue-control.py invocation pays.
"""

import asyncio
import importlib.util
import os
import sys
import termios
import tty
from pathlib import Path

def sibling(filename, name):
    """Import a hyphenated script sitting next to this one; plain import can't."""
    spec = importlib.util.spec_from_file_location(
        name, Path(__file__).with_name(filename)
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# Reuse ADDR and the hand-tuned COLORS/EFFECTS tables rather than copying them.
huectl = sibling("hue-control.py", "huectl")
alarms = sibling("hue-alarms.py", "huealarms")
fader = sibling("hue-fade.py", "huefade")

ADDR = huectl.ADDR
COLORS = huectl.COLORS
EFFECTS = huectl.EFFECTS
EffectType = huectl.EffectType

from bleak import BleakScanner
from HueBLE import HueBleLight

# This firmware can report effect ids HueBLE's enum does not define (0x0D is one).
# EffectType(raw) then raises inside poll_effects, every poll fails, and the light
# becomes unreachable until something writes a known effect back. Treat unknown ids
# as "no effect" so a stray value degrades the display instead of locking us out.
EffectType._missing_ = classmethod(lambda cls, value: cls.NONE)

BRI_PCT_STEP = 10  # brightness moves in whole 10% stops
BRI_MAX = 254
TEMP_MIN, TEMP_MAX = 153, 500
DEFAULT_TEMP = 370  # fallback when the bulb reports a temp outside the valid range

# Keys that do something on the main screen. Listed here so the colour pool below
# can exclude them: 'a' used to be both amber and the alarms key, which made amber
# unreachable, and deriving the pool stops that recurring as keys get added.
ACTION_KEYS = set(" aeEfhjklqr")

# One key per colour, so the list can grow past ten without colliding.
INDEX_KEYS = "".join(
    c for c in "1234567890abcdefghijklmnopqrstuvwxyz" if c not in ACTION_KEYS
)[: len(COLORS)]

DIM = "\033[2m"
OFF = "\033[0m"
BOLD = "\033[1m"

# "none" cannot be selected through the effects characteristic: effect=0 is
# accepted and then ignored, and HueBLE's set_colour_effect() omits the effect
# field entirely for EffectType.NONE (HueBLE.py:1267) so the bulb is never told to
# stop. Writing the temperature characteristic does stop it, so cycling to "none"
# is special-cased to that — at the cost of dropping back to white.
CYCLE_EFFECTS = list(EFFECTS)

# Arrows are the primary bindings; the vim keys stay as aliases.
BRIGHTER = {"up", "k"}
DIMMER = {"down", "j"}
WARMER = {"right", "l"}
COOLER = {"left", "h"}
ARROWS = {"A": "up", "B": "down", "C": "right", "D": "left"}


def clamp(value, lo, hi):
    return max(lo, min(hi, value))


def to_pct(raw):
    return clamp(round((raw or 0) / BRI_MAX * 100), 0, 100)


def from_pct(pct):
    return clamp(round(pct / 100 * BRI_MAX), 1, BRI_MAX)


def step_pct(pct, direction, floor=BRI_PCT_STEP):
    """Next 10% stop from `pct`, snapped to the ladder first.

    Brightness floors at one step because 0% is what the power toggle is for;
    colour temperature floors at 0, which is simply the coolest white.
    """
    stop = round(pct / BRI_PCT_STEP) * BRI_PCT_STEP
    return clamp(stop + direction * BRI_PCT_STEP, floor, 100)


def temp_to_pct(mireds):
    """Mireds -> 0-100%, where 0% is the coolest the bulb goes and 100% the warmest."""
    span = TEMP_MAX - TEMP_MIN
    return clamp(round((mireds - TEMP_MIN) / span * 100), 0, 100)


def pct_to_temp(pct):
    return clamp(round(TEMP_MIN + pct / 100 * (TEMP_MAX - TEMP_MIN)), TEMP_MIN, TEMP_MAX)


def shown_temp_pct(light):
    """Colour temperature to display and step from, held the same way as brightness."""
    intent = getattr(light, "ui_temp_pct", None)
    if intent is not None:
        return intent
    mireds = light.colour_temp
    if not mireds or not TEMP_MIN <= mireds <= TEMP_MAX:
        return temp_to_pct(DEFAULT_TEMP)
    return temp_to_pct(mireds)


def shown_pct(light):
    """Brightness to display and to step from.

    The bulb fades to a new brightness rather than jumping, so a read taken
    right after a write catches it mid-fade and reports a value that lags the
    target. Stepping from that lagging number produces uneven jumps (100 -> 96
    -> 91), so hold the intended stop and only resync from the bulb on an
    explicit refresh or a reconnect.
    """
    intent = getattr(light, "ui_bri_pct", None)
    return intent if intent is not None else to_pct(light.brightness)


def xy_to_rgb(x, y):
    """CIE xy -> 8-bit sRGB, at full luminance. Philips wide-gamut matrix."""
    if y <= 0:
        return (0, 0, 0)
    X, Z = x / y, (1 - x - y) / y
    rgb = (
        X * 1.656492 - 0.354851 - Z * 0.255038,
        -X * 0.707196 + 1.655397 + Z * 0.036152,
        X * 0.051713 - 0.121364 + Z * 1.011530,
    )
    rgb = [max(0.0, c) for c in rgb]
    peak = max(rgb) or 1.0
    out = []
    for c in rgb:
        c /= peak  # normalise so every swatch renders at full vividness
        c = 1.055 * c ** (1 / 2.4) - 0.055 if c > 0.0031308 else 12.92 * c
        out.append(int(clamp(round(c * 255), 0, 255)))
    return tuple(out)


def fg(rgb):
    return "\033[38;2;{};{};{}m".format(*rgb)


def swatch(name):
    return f"{fg(xy_to_rgb(*COLORS[name]))}●{OFF}"


def bar(value, maximum, width=20):
    filled = int(round(width * clamp(value, 0, maximum) / maximum))
    return "█" * filled + f"{DIM}{'─' * (width - filled)}{OFF}"


def colour_at(index):
    """Colour name for a 0-based index, or None if out of range."""
    names = list(COLORS)
    return names[index] if 0 <= index < len(names) else None


def next_name(names, current, step=1):
    """Entry `step` places from `current`, wrapping. Unknown current -> first entry."""
    names = list(names)
    if current not in names:
        return names[0]
    return names[(names.index(current) + step) % len(names)]


def effect_name(light):
    if not light.supports_effects or not light.effect:
        return "none"
    return {v: k for k, v in EFFECTS.items()}.get(light.effect[0], "none")


def parse_keys(buf):
    """Split raw stdin text into key tokens. Returns (keys, leftover).

    An arrow press arrives as ESC [ A, so bytes have to be read in blocks and
    decoded rather than one at a time. A block can also end mid-sequence, so any
    incomplete tail is handed back to be prepended to the next read.
    """
    keys, i = [], 0
    while i < len(buf):
        ch = buf[i]
        if ch in ("\x7f", "\b"):
            keys.append("backspace")
            i += 1
            continue
        if ch != "\033":
            keys.append(ch)
            i += 1
            continue
        if i + 1 >= len(buf):
            # Bare ESC with nothing after it. Terminals emit an arrow's three
            # bytes in one write, so this is a real Escape press, not a torn
            # sequence — resolve it now or Escape could never be pressed at all.
            keys.append("escape")
            i += 1
            continue
        if buf[i + 1] != "[":
            i += 2  # some other escape, drop it
            continue
        end = i + 2
        while end < len(buf) and not ("@" <= buf[end] <= "~"):
            end += 1
        if end >= len(buf):
            return keys, buf[i:]  # sequence not finished yet
        if buf[end] in ARROWS:
            keys.append(ARROWS[buf[end]])
        i = end + 1  # unrecognised sequences are swallowed, not typed through
    return keys, ""


def valid_xy(xy):
    """True if xy is a real chromaticity, not the (1.0, 1.0) sentinel.

    In temperature mode the bulb reports 0xFFFF/0xFFFF for both coordinates.
    Packing that back into a write produces an invalid colour and the bulb
    rejects the whole command, so every write path has to check first.
    """
    if not xy:
        return False
    x, y = xy
    return x > 0 and y > 0 and x + y <= 1.0


def colour_name(light):
    """Closest named colour to the bulb's current XY, or '-' if unknown."""
    if not valid_xy(light.colour_xy):
        return "-"
    x, y = light.colour_xy
    return min(COLORS, key=lambda n: (COLORS[n][0] - x) ** 2 + (COLORS[n][1] - y) ** 2)


def wrap(cells, per_row, indent):
    """Lay out (visible_width, styled_text) cells in aligned columns.

    Padding is computed from the visible width because the styled text carries
    ANSI escapes that len() would count as characters.
    """
    width = max(w for w, _ in cells)
    rows = [cells[i : i + per_row] for i in range(0, len(cells), per_row)]
    return [
        (indent + "".join(text + " " * (width - w + 2) for w, text in row)).rstrip()
        for row in rows
    ]


def render(light, status):
    current = colour_name(light)
    bri = shown_pct(light)
    temp = shown_temp_pct(light)
    active_effect = effect_name(light)

    if light.power_state:
        tint = fg(xy_to_rgb(*COLORS[current])) if current in COLORS else "\033[33m"
        power = f"{tint}{BOLD}● ON {OFF}"
    else:
        tint = DIM
        power = f"{DIM}○ OFF{OFF}"

    colours = [
        (
            len(n) + 3,
            f"{DIM}{INDEX_KEYS[i]}{OFF} {swatch(n)}"
            + (f"{BOLD}{n}{OFF}" if n == current and light.power_state
               else f"{DIM}{n}{OFF}"),
        )
        for i, n in enumerate(COLORS)
    ]
    effects = [
        (len(n) + 2, f"\033[7m {n} {OFF}") if n == active_effect
        else (len(n) + 2, f" {DIM}{n}{OFF} ")
        for n in CYCLE_EFFECTS
    ]

    out = [
        "\033[2J\033[H",
        "",
        f"  {BOLD}Hue{OFF} {DIM}·{OFF} desk light{' ' * 34}{DIM}{status}{OFF}",
        "",
        f"  {power}",
        "",
        f"    {DIM}bright{OFF}  {tint}{bar(bri, 100)}{OFF} {bri:>3}%",
        f"    {DIM}temp{OFF}    {bar(temp, 100)} {temp:>3}%",
        f"            {DIM}cool ·········· warm{OFF}",
        "",
        f"  {DIM}colours{OFF}",
        *wrap(colours, 4, "    "),
        "",
        f"  {DIM}effects{OFF}  {DIM}e/E cycles · none returns to white{OFF}",
        *wrap(effects, 4, "    "),
        "",
        f"  {DIM}↑/↓{OFF} bright  {DIM}←/→{OFF} temp  {DIM}space{OFF} toggle  "
        f"{DIM}e/E{OFF} effect  {DIM}f{OFF} flash  {DIM}a{OFF} alarms  "
        f"{DIM}r{OFF} refresh  {DIM}q{OFF} quit",
        f"  {DIM}colours {INDEX_KEYS[0]}–{INDEX_KEYS[-1]} · arrows also as j/k/h/l{OFF}",
        "",
    ]
    sys.stdout.write("\n".join(out))
    sys.stdout.flush()


def render_alarms(state, status):
    """Alarm list, plus the add-wizard prompt when one is in progress."""
    rows = []
    for i, alarm in enumerate(state["list"]):
        mark = "●" if alarm.get("enabled", True) else f"{DIM}○{OFF}"
        line = alarms.describe(alarm)
        fires = f"{DIM}fires {alarms.fire_time(alarm)}{OFF}" \
            if alarm.get("fade") else ""
        cursor = f"{BOLD}›{OFF}" if i == state["cursor"] else " "
        body = f"{BOLD}{line}{OFF}" if i == state["cursor"] else f"{DIM}{line}{OFF}"
        rows.append(f"  {cursor} {mark} {body}  {fires}")
    if not rows:
        rows = [f"  {DIM}no alarms yet — press n{OFF}"]

    scenes = fader.load_scenes()
    scene_rows = [
        (len(name) + len(fader.describe_scene(scene)) + 2,
         f"{BOLD}{name}{OFF} {DIM}{fader.describe_scene(scene)}{OFF}")
        for name, scene in scenes.items()
    ]

    out = [
        "\033[2J\033[H",
        "",
        f"  {BOLD}Alarms{OFF}{' ' * 42}{DIM}{status}{OFF}",
        "",
        *rows,
        "",
        f"  {DIM}scenes{OFF}",
        *wrap(scene_rows, 2, "    "),
        "",
    ]
    if state["prompt"]:
        out += [f"  {BOLD}{state['prompt']}{OFF}  {state['typed']}▏", ""]
    else:
        out += [
            f"  {DIM}↑/↓{OFF} select  {DIM}n{OFF} new  {DIM}d{OFF} delete  "
            f"{DIM}space{OFF} enable  {DIM}s{OFF} save light as scene  {DIM}a{OFF} back",
            "",
        ]
    sys.stdout.write("\n".join(out))
    sys.stdout.flush()


# The add-wizard, as a list of steps. Each step names the field it fills, the
# prompt, and how to turn keystrokes into a value. Kept declarative so adding a
# field later is one entry, not another branch in a growing if-chain.
def scene_keys():
    """Number keys onto scene names, in the order they appear in the scenes file."""
    return {str(i + 1): name for i, name in enumerate(fader.load_scenes()) if i < 9}


def wizard_steps(draft=None):
    draft = draft or {}
    picker = "  ".join(f"{k} {v}" for k, v in scene_keys().items())
    steps = [
        ("time", "time  HH:MM (4 digits)", "digits4"),
        ("days", "repeat  1 daily  2 weekdays  3 weekend",
         {"1": "daily", "2": "weekdays", "3": "weekend"}),
        ("scene", f"scene  {picker}", scene_keys()),
        ("fade", "fade minutes, 00 for an instant switch", "digits2"),
    ]
    if draft.get("fade"):
        steps.append(("anchor", "fade  e ends at that time  s starts at it",
                      {"e": "end", "s": "start"}))
    return steps


def wizard_key(state, key):
    """Feed one keystroke to the add-wizard. Returns True when it finishes."""
    steps = wizard_steps(state["draft"])
    field, prompt, kind = steps[state["step"]]
    state["prompt"] = prompt

    if key == "escape":
        state.update(prompt=None, step=0, draft={}, typed="")
        return False

    if kind in ("digits4", "digits2"):
        want = 4 if kind == "digits4" else 2
        if key.isdigit():
            state["typed"] += key
        elif key == "backspace":
            state["typed"] = state["typed"][:-1]
        if len(state["typed"]) < want:
            return False
        value = state["typed"]
        state["typed"] = ""
        state["draft"][field] = f"{value[:2]}:{value[2:]}" if want == 4 else int(value)
    else:
        if key not in kind:
            return False
        state["draft"][field] = kind[key]

    state["step"] += 1
    remaining = wizard_steps(state["draft"])
    if state["step"] < len(remaining):
        # advance the prompt now; the screen is drawn before the next keypress
        state["prompt"] = remaining[state["step"]][1]
        return False

    draft = state["draft"]
    alarms.add(draft["time"], draft["days"], draft["scene"],
               draft.get("fade", 0), draft.get("anchor", "end"))
    state.update(prompt=None, step=0, draft={}, typed="")
    return True


def name_key(state, key, light):
    """Collect a scene name a character at a time. Returns True when saved."""
    state["prompt"] = "scene name, then enter"
    if key == "escape":
        state.update(prompt=None, naming=False, typed="")
        return False
    if key == "backspace":
        state["typed"] = state["typed"][:-1]
        return False
    if key in ("\r", "\n"):
        name = state["typed"].strip()
        if name:
            fader.store_scene(name, fader.scene_from_state(fader.read_state(light)))
        state.update(prompt=None, naming=False, typed="")
        return bool(name)
    if len(key) == 1 and (key.isalnum() or key in "-_"):
        state["typed"] += key
    return False


def alarm_key(state, key, light=None):
    """Handle a keystroke on the alarm screen. Returns False to go back."""
    if state.get("naming"):
        name_key(state, key, light)
        return True
    if state["prompt"] is not None:
        if wizard_key(state, key):
            state["list"] = alarms.load()
        return True

    entries = state["list"]
    if key in DIMMER and entries:
        state["cursor"] = (state["cursor"] + 1) % len(entries)
    elif key in BRIGHTER and entries:
        state["cursor"] = (state["cursor"] - 1) % len(entries)
    elif key == "n":
        state.update(prompt="", step=0, draft={}, typed="")
        wizard_key(state, "")  # render the first prompt without consuming a key
    elif key == "s" and light is not None:
        state.update(naming=True, typed="", prompt="scene name, then enter")
    elif key == "d" and entries:
        alarms.remove(entries[state["cursor"]]["id"])
        state["list"] = alarms.load()
        state["cursor"] = max(0, min(state["cursor"], len(state["list"]) - 1))
    elif key == " " and entries:
        alarms.toggle(entries[state["cursor"]]["id"])
        state["list"] = alarms.load()
    elif key in ("a", "q", "escape"):
        return False
    return True


async def refresh(light):
    """Re-read just the state that can change.

    poll_state() costs ~10 GATT reads because it also fetches manufacturer,
    model, firmware, ZigBee address and name — all static for the session.
    poll_effects() returns power, brightness, colour and effect in a single
    read; it skips brightness in temperature-without-effect mode, so pick that
    one up separately.

    Clearing _effect first is what makes "no effect" detectable: poll_effects()
    only assigns it when the bulb returns the long, effect-carrying response, and
    it never resets it when the effect is gone — so a stale effect would otherwise
    stay on screen forever.
    """
    light._effect = None
    await light.poll_effects()
    await light.poll_brightness()


async def connect():
    light = await huectl.acquire_light()
    await light.poll_state()  # once, for the supports_* flags and static info
    return light


async def apply(light, key):
    """Run the action for `key`. Returns False to quit."""
    if key == "q":
        return False

    elif key == "r":
        # drop held values, take whatever the bulb says
        light.ui_bri_pct = None
        light.ui_temp_pct = None

    elif key == " ":
        await light.set_power(not light.power_state)

    elif key in BRIGHTER | DIMMER:
        pct = step_pct(shown_pct(light), 1 if key in BRIGHTER else -1)
        await light.set_brightness(from_pct(pct))
        light.ui_bri_pct = pct

    elif key in WARMER | COOLER:
        pct = step_pct(shown_temp_pct(light), 1 if key in WARMER else -1, floor=0)
        await light.set_colour_temp(pct_to_temp(pct))
        light.ui_temp_pct = pct

    elif key == "f":
        scene = fader.load_scenes().get("alert", fader.DEFAULT_SCENES["alert"])
        await fader.run_flash(light, scene)

    elif key in INDEX_KEYS:
        await light.set_colour_xy(*COLORS[colour_at(INDEX_KEYS.index(key))])

    elif key in ("e", "E"):
        name = next_name(CYCLE_EFFECTS, effect_name(light), step=1 if key == "e" else -1)
        bri = light.brightness or 128
        temp = light.colour_temp
        if not temp or not TEMP_MIN <= temp <= TEMP_MAX:
            temp = DEFAULT_TEMP
        if name == "none":
            # only a write to the temperature characteristic stops a running effect
            await light.set_colour_temp(temp)
        elif light.supports_colour_xy and valid_xy(light.colour_xy):
            x, y = light.colour_xy
            await light.set_colour_effect(x, y, bri, EFFECTS[name], 128)
        else:
            await light.set_temperature_effect(temp, bri, EFFECTS[name], 128)

    await refresh(light)
    return True


async def main():
    fd = sys.stdin.fileno()
    saved = termios.tcgetattr(fd)
    tty.setcbreak(fd)

    keys = asyncio.Queue()
    loop = asyncio.get_running_loop()
    pending = ""

    def on_readable():
        nonlocal pending
        chunk = os.read(fd, 32).decode("utf-8", "ignore")
        parsed, pending = parse_keys(pending + chunk)
        for key in parsed:
            keys.put_nowait(key)

    loop.add_reader(fd, on_readable)

    sys.stdout.write("\033[?25l")  # hide cursor
    light = None
    try:
        print("connecting…")
        light = await connect()
        status = "connected"

        alarm_state = {"list": alarms.load(), "cursor": 0, "prompt": None,
                       "step": 0, "draft": {}, "typed": ""}

        while True:
            if alarm_state["prompt"] is not None or alarm_state.get("open"):
                render_alarms(alarm_state, status)
                key = await keys.get()
                if not alarm_key(alarm_state, key, light):
                    alarm_state["open"] = False
                continue

            render(light, status)
            key = await keys.get()
            status = "connected"
            if key == "a":
                alarm_state.update(open=True, list=alarms.load(), cursor=0)
                continue
            try:
                if not await apply(light, key):
                    break
            except Exception as e:
                # BLE links drop. Retry up to 3 times with fresh scans.
                for attempt in range(3):
                    render(light, f"reconnecting {attempt+1}/3…")
                    try:
                        await light.disconnect()
                    except Exception:
                        pass
                    try:
                        light = await connect()
                        await apply(light, key)
                        status = "reconnected"
                        break
                    except Exception as e2:
                        if attempt == 2:
                            status = f"GATT error — power-cycle the bulb"
                        else:
                            await asyncio.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        loop.remove_reader(fd)
        termios.tcsetattr(fd, termios.TCSADRAIN, saved)
        sys.stdout.write("\033[?25h\033[2J\033[H")
        sys.stdout.flush()
        if light:
            try:
                await light.disconnect()
            except Exception:
                pass


def demo():
    assert clamp(300, 1, 254) == 254
    assert clamp(-5, 1, 254) == 1
    assert clamp(100, 1, 254) == 100
    assert clamp(600, TEMP_MIN, TEMP_MAX) == TEMP_MAX

    names = list(COLORS)
    assert colour_at(0) == names[0]
    assert colour_at(len(names) - 1) == names[-1]
    assert colour_at(len(names)) is None
    assert colour_at(-1) is None

    assert next_name(names, names[0]) == names[1]
    assert next_name(names, names[-1]) == names[0]  # wraps forward
    assert next_name(names, names[0], step=-1) == names[-1]  # wraps back
    assert next_name(names, "nonexistent") == names[0]

    assert CYCLE_EFFECTS[0] == "none"  # reachable again, via the temperature write
    assert next_name(CYCLE_EFFECTS, "none") == "candle"
    assert next_name(CYCLE_EFFECTS, "enchant") == "none"  # wraps forward, back to off
    assert next_name(CYCLE_EFFECTS, "none", step=-1) == "enchant"  # and back
    # every effect, off included, is reachable by pressing e repeatedly
    seen, cur = set(), "none"
    for _ in CYCLE_EFFECTS:
        cur = next_name(CYCLE_EFFECTS, cur)
        seen.add(cur)
    assert seen == set(EFFECTS)

    # every colour has its own key, and no key collides with an action
    assert len(INDEX_KEYS) == len(names), "ran out of index keys for COLORS"
    assert not set(INDEX_KEYS) & set("cehjklqrE ")
    assert {colour_at(i) for i in range(len(INDEX_KEYS))} == set(names)

    for name, (x, y) in COLORS.items():
        r, g, b = xy_to_rgb(x, y)
        assert all(0 <= c <= 255 for c in (r, g, b)), name
        assert max(r, g, b) > 200, f"{name} renders too dark to see"
    assert xy_to_rgb(0.3, 0.0) == (0, 0, 0)  # degenerate y, no divide-by-zero
    r, g, b = xy_to_rgb(*COLORS["white"])
    assert max(r, g, b) - min(r, g, b) < 60, "white swatch should look neutral"
    assert xy_to_rgb(*COLORS["red"])[0] > xy_to_rgb(*COLORS["blue"])[0]

    # brightness always lands on the same 10% ladder
    assert step_pct(70, 1) == 80
    assert step_pct(70, -1) == 60
    assert step_pct(96, -1) == 90  # a mid-fade reading still snaps to the ladder
    assert step_pct(91, -1) == 80
    assert step_pct(100, 1) == 100  # no overshoot at the top
    assert step_pct(0, -1) == BRI_PCT_STEP  # never steps to 0 (that is what OFF is for)
    pct, walked = 100, []
    for _ in range(9):
        pct = step_pct(pct, -1)
        walked.append(pct)
    assert walked == [90, 80, 70, 60, 50, 40, 30, 20, 10], walked
    assert to_pct(0) == 0 and to_pct(BRI_MAX) == 100
    assert 1 <= from_pct(0) and from_pct(100) == BRI_MAX

    # held intent wins over the lagging read-back, and 'r' drops it
    class FakeLight:
        brightness = 243  # bulb still fading down toward the 90% we asked for
    fake = FakeLight()
    assert shown_pct(fake) == 96  # nothing held yet, so report what the bulb says
    fake.ui_bri_pct = 90
    assert shown_pct(fake) == 90
    assert step_pct(shown_pct(fake), -1) == 80  # steps from intent, not from 96
    fake.ui_bri_pct = None
    assert shown_pct(fake) == 96

    # colour temp reads as 0-100%, not raw mireds: coolest is 0, not 153
    assert temp_to_pct(TEMP_MIN) == 0
    assert temp_to_pct(154) == 0  # what the screenshot showed as a bare "154"
    assert temp_to_pct(TEMP_MAX) == 100
    assert pct_to_temp(0) == TEMP_MIN and pct_to_temp(100) == TEMP_MAX
    assert temp_to_pct(pct_to_temp(40)) == 40  # round-trips on the ladder
    assert step_pct(0, -1, floor=0) == 0  # cool end stops at 0, not at 10
    assert step_pct(0, 1, floor=0) == 10

    # arrow keys arrive as escape sequences and must survive being split up
    assert parse_keys("\033[A") == (["up"], "")
    assert parse_keys("\033[B\033[C\033[D") == (["down", "right", "left"], "")
    assert parse_keys("k") == (["k"], "")
    assert parse_keys(" q") == ([" ", "q"], "")
    assert parse_keys("\033") == (["escape"], "")  # bare ESC is a real keypress
    assert parse_keys("\033[") == ([], "\033[")  # but ESC [ might still be an arrow
    first, leftover = parse_keys("j\033[")
    assert first == ["j"] and leftover == "\033["
    assert parse_keys("\x7f") == (["backspace"], "")
    assert parse_keys(leftover + "A") == (["up"], "")  # resumes across reads
    assert parse_keys("\033[5~") == ([], "")  # unknown sequence swallowed whole
    assert parse_keys("\033[2;3R k") == ([" ", "k"], "")  # no stray digits typed through

    # no binding is claimed by two actions
    assert not ACTION_KEYS & set(INDEX_KEYS)  # amber vs alarms: caught exactly this
    assert not (BRIGHTER | DIMMER | WARMER | COOLER) & set(INDEX_KEYS)
    assert len(INDEX_KEYS) == len(COLORS), "colour pool ran out of free keys"
    assert (BRIGHTER | DIMMER | WARMER | COOLER) - {"up", "down", "left", "right"} \
        <= ACTION_KEYS  # the vim aliases must be declared as actions too
    for a, b in ((BRIGHTER, DIMMER), (WARMER, COOLER), (BRIGHTER, WARMER)):
        assert not a & b

    assert not valid_xy((1.0, 1.0))  # temperature-mode sentinel
    assert not valid_xy(None)
    assert not valid_xy((0.0, 0.4))
    assert not valid_xy((0.7, 0.7))  # x + y > 1
    assert all(valid_xy(xy) for xy in COLORS.values())

    assert bar(254, 254, 10).count("█") == 10
    assert bar(0, 254, 10).count("█") == 0
    assert bar(-99, 254, 10).count("█") == 0

    print("selftest ok")


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        demo()
    else:
        asyncio.run(main())
