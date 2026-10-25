#!/home/linuxbrew/.linuxbrew/bin/python3.14
"""Alarm schedule for the Hue bulb.

Alarms live in a JSON file and are realised as systemd user timers, matching the
media-* timer pattern already on this machine: they survive reboot and fire
whether or not the TUI is open.

Every alarm is "move to scene X over N minutes". N=0 is an instant switch, and
the scene can be any entry in ~/.config/hue-scenes.json including "off", so a
sunrise wake, a switch to work light, and a fade to off are all the same shape.

A fade is anchored either to its end (be awake *by* 07:00) or its start (begin
winding down *at* 22:00), which decides when the timer actually fires.

Usage:
  hue-alarms.py list
  hue-alarms.py add <HH:MM> <daily|weekdays|weekend> <scene> [minutes] [end|start]
  hue-alarms.py rm <id>
  hue-alarms.py toggle <id>
  hue-alarms.py sync
"""

import json
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

STORE = Path.home() / ".config" / "hue-alarms.json"
UNIT_DIR = Path.home() / ".config" / "systemd" / "user"
PREFIX = "hue-alarm-"
BIN = Path.home() / "bin"

# systemd OnCalendar day specs
DAYS = {"daily": "", "weekdays": "Mon..Fri ", "weekend": "Sat,Sun "}


def load():
    if not STORE.exists():
        return []
    return json.loads(STORE.read_text())


def save(alarms):
    STORE.parent.mkdir(parents=True, exist_ok=True)
    STORE.write_text(json.dumps(alarms, indent=2) + "\n")


def next_id(alarms):
    return max((a["id"] for a in alarms), default=0) + 1


def fire_time(alarm):
    """When the timer fires.

    An end-anchored fade has to *finish* at the alarm time, so it starts that
    many minutes earlier and can wrap back past midnight.
    """
    when = datetime.strptime(alarm["time"], "%H:%M")
    if alarm.get("anchor", "end") == "end":
        when -= timedelta(minutes=alarm.get("fade", 0))
    return when.strftime("%H:%M")


def oncalendar(alarm):
    return f"{DAYS[alarm['days']]}{fire_time(alarm)}:00"


def exec_start(alarm):
    return f"{BIN / 'hue-fade.py'} {alarm['scene']} {alarm.get('fade', 0)}"


def describe(alarm):
    fade = alarm.get("fade", 0)
    if not fade:
        what = f"→ {alarm['scene']}"
    else:
        anchor = "by" if alarm.get("anchor", "end") == "end" else "from"
        what = f"→ {alarm['scene']} over {fade}m {anchor}"
    return f"{alarm['time']}  {alarm['days']:<9} {what}"


def unit_text(alarm):
    name = f"Hue alarm {alarm['id']}: {describe(alarm)}"
    service = (
        f"[Unit]\nDescription={name}\n\n"
        f"[Service]\nType=oneshot\nExecStart={exec_start(alarm)}\n"
    )
    timer = (
        f"[Unit]\nDescription={name}\n\n"
        f"[Timer]\nOnCalendar={oncalendar(alarm)}\nPersistent=false\n\n"
        f"[Install]\nWantedBy=timers.target\n"
    )
    return service, timer


def systemctl(*args, check=False):
    return subprocess.run(
        ["systemctl", "--user", *args], capture_output=True, text=True, check=check
    )


def sync():
    """Make the unit files and their enabled state match the store exactly."""
    UNIT_DIR.mkdir(parents=True, exist_ok=True)
    alarms = load()
    wanted = {f"{PREFIX}{a['id']}" for a in alarms}

    for path in UNIT_DIR.glob(f"{PREFIX}*"):
        if path.stem not in wanted:
            systemctl("disable", "--now", f"{path.stem}.timer")
            path.unlink()

    for alarm in alarms:
        stem = f"{PREFIX}{alarm['id']}"
        service, timer = unit_text(alarm)
        (UNIT_DIR / f"{stem}.service").write_text(service)
        (UNIT_DIR / f"{stem}.timer").write_text(timer)

    systemctl("daemon-reload")
    for alarm in alarms:
        unit = f"{PREFIX}{alarm['id']}.timer"
        if alarm.get("enabled", True):
            systemctl("enable", "--now", unit)
        else:
            systemctl("disable", "--now", unit)
    return alarms


def add(time_str, days, scene, fade=0, anchor="end"):
    alarms = load()
    alarm = {"id": next_id(alarms), "time": time_str, "days": days, "scene": scene,
             "fade": int(fade), "anchor": anchor, "enabled": True}
    alarms.append(alarm)
    save(alarms)
    sync()
    return alarm


def remove(alarm_id):
    save([a for a in load() if a["id"] != alarm_id])
    sync()


def toggle(alarm_id):
    alarms = load()
    for a in alarms:
        if a["id"] == alarm_id:
            a["enabled"] = not a.get("enabled", True)
    save(alarms)
    sync()


def demo():
    instant = {"time": "09:00", "days": "weekdays", "scene": "day", "fade": 0}
    assert oncalendar(instant) == "Mon..Fri 09:00:00"
    assert oncalendar({**instant, "days": "daily"}) == "09:00:00"
    assert oncalendar({**instant, "days": "weekend"}) == "Sat,Sun 09:00:00"

    # end-anchored: be awake BY 07:00, so a 30m sunrise starts at 06:30
    wake = {"time": "07:00", "days": "weekdays", "scene": "wake",
            "fade": 30, "anchor": "end"}
    assert fire_time(wake) == "06:30"
    # ...and that still works when it wraps back past midnight
    assert fire_time({**wake, "time": "00:15"}) == "23:45"
    # start-anchored: begin winding down AT 22:00, ending 45m later
    assert fire_time({"time": "22:00", "scene": "night",
                      "fade": 45, "anchor": "start"}) == "22:00"
    assert fire_time(instant) == "09:00"  # no fade, no offset either way

    service, timer = unit_text({"id": 1, **wake})
    assert "OnCalendar=Mon..Fri 06:30:00" in timer
    assert "WantedBy=timers.target" in timer
    assert "hue-fade.py wake 30" in service
    assert "Type=oneshot" in service

    off = unit_text({"id": 2, "time": "23:30", "days": "daily", "scene": "off",
                     "fade": 20, "anchor": "start"})
    assert "hue-fade.py off 20" in off[0]  # fade to off is just another scene
    assert "OnCalendar=23:30:00" in off[1]

    assert next_id([]) == 1
    assert next_id([{"id": 1}, {"id": 4}]) == 5  # no reuse after a delete
    assert describe(wake) == "07:00  weekdays  → wake over 30m by"
    assert describe(instant) == "09:00  weekdays  → day"
    print("selftest ok")


def main():
    args = sys.argv[1:]
    cmd = args[0] if args else "list"

    if cmd == "--selftest":
        demo()
    elif cmd == "list":
        alarms = load()
        if not alarms:
            print("no alarms")
        for a in alarms:
            mark = "●" if a.get("enabled", True) else "○"
            print(f"{a['id']:>3} {mark} {describe(a)}")
    elif cmd == "add":
        time_str, days, scene = args[1], args[2], args[3]
        fade = int(args[4]) if len(args) > 4 else 0
        anchor = args[5] if len(args) > 5 else "end"
        print("added:", describe(add(time_str, days, scene, fade, anchor)))
    elif cmd == "rm":
        remove(int(args[1]))
        print("removed")
    elif cmd == "toggle":
        toggle(int(args[1]))
        print("toggled")
    elif cmd == "sync":
        print(f"synced {len(sync())} alarm(s)")
    else:
        print(__doc__)
        sys.exit(1)


if __name__ == "__main__":
    main()
