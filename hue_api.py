#!/usr/bin/env python3
"""Hue bridge API helper — reads credentials from file, never exposes
the username in process arguments."""

import contextlib
import fcntl
import json
import os
import re
import socket
import ssl
import stat
import sys
import tempfile
import time
import urllib.request

def _xdg_state_home():
    return os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state")


def _xdg_config_home():
    return os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")


def _resolve_cacert(config_home):
    # Prefer the copy sitting next to this script -- makes a dev clone or
    # standalone test run work without also having a live install present.
    # During real operation this always resolves to the same file the
    # install-path fallback would have anyway, since Quickshell always
    # invokes this script from within the live install directory.
    script_dir = os.path.dirname(os.path.abspath(__file__))
    local_cacert = os.path.join(script_dir, "hue_bridge_cacert.pem")
    if os.path.isfile(local_cacert):
        return local_cacert
    return os.path.join(
        config_home,
        "omarchy/plugins/lunardi0x01.hue-room-remote/hue_bridge_cacert.pem")


STATE_HOME = _xdg_state_home()
CONFIG_HOME = _xdg_config_home()
CREDS_FILE = os.path.join(STATE_HOME, "omarchy/settings/hue.json")
CACERT = _resolve_cacert(CONFIG_HOME)

MAX_CREDS_BYTES = 4096
MAX_ORDER_BYTES = 65536
MAX_RESPONSE_BYTES = 1024 * 1024

_opener = None
_no_redirect_opener = None


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Refuses to follow redirects on credential-bearing requests -- a
    compromised bridge could otherwise 302 an authenticated request (with
    the username embedded in the URL path) to an attacker-controlled
    host."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _get_opener():
    global _opener
    if _opener is None:
        ctx = ssl.create_default_context(cafile=CACERT)
        ctx.check_hostname = True
        _opener = urllib.request.build_opener(
            urllib.request.HTTPSHandler(context=ctx), _NoRedirectHandler())
    return _opener


def _get_no_redirect_opener():
    global _no_redirect_opener
    if _no_redirect_opener is None:
        # Insecure mode (no bridgeId yet, connecting straight to the bridge's
        # IP): the cert's SAN names the bridge id, not the IP, so hostname
        # checking can never pass here -- but the cert should still chain to
        # our bundled root. Pinning cafile=CACERT with check_hostname=False
        # (rather than disabling verification outright) still rejects a
        # cert from an unrelated/untrusted source.
        ctx = ssl.create_default_context(cafile=CACERT)
        ctx.check_hostname = False
        _no_redirect_opener = urllib.request.build_opener(
            urllib.request.HTTPSHandler(context=ctx), _NoRedirectHandler())
    return _no_redirect_opener


class _BridgeResolver:
    def __init__(self, hostname, ip):
        self._hostname = hostname
        self._ip = ip
        self._orig = None

    def __enter__(self):
        self._orig = socket.getaddrinfo
        def _patched(host, port, *args, **kwargs):
            if host == self._hostname:
                return [(socket.AF_INET, socket.SOCK_STREAM, 6, '',
                         (self._ip, port))]
            return self._orig(host, port, *args, **kwargs)
        socket.getaddrinfo = _patched
        return self

    def __exit__(self, *args):
        socket.getaddrinfo = self._orig


def _read_local_json_capped(path, max_bytes):
    # O_NONBLOCK makes open() return immediately even if `path` is a FIFO
    # with no writer -- without it, a named pipe planted at a predictable
    # settings path would hang this call (and everything waiting on it)
    # indefinitely. It has no effect on a regular file. The S_ISREG check
    # below then rejects a FIFO/socket/device before any read is attempted.
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd) as f:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise ValueError("not a regular file")
        if st.st_uid != os.getuid():
            raise ValueError("not owned by current user")
        data = f.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise ValueError("file too large")
    return json.loads(data)


def _load_creds():
    return _read_local_json_capped(CREDS_FILE, MAX_CREDS_BYTES)


_BRIDGE_ID_RE = re.compile(r'[0-9a-f]{16}')


def _read_bridge_id(creds):
    # Single canonical bridgeId validator, shared by _get_status (what
    # panel.qml's insecureMode warning is driven by) and _bridge_url (what
    # actually decides whether a request gets hostname verification). These
    # used to be checked against two different regexes -- a malformed but
    # alnum id could pass _get_status's old looser check and read as
    # "verified" in the UI while _bridge_url rejected the same value and
    # silently fell back to the no-hostname-verification path. Guarding
    # with isinstance also means an explicit `"bridgeId": null` in a
    # hand-edited hue.json returns "" here instead of raising (creds.get's
    # default only applies when the key is absent, not when it's present
    # with value None).
    bridge_id = creds.get("bridgeId")
    if not isinstance(bridge_id, str):
        return ""
    bridge_id = bridge_id.strip().lower()
    return bridge_id if _BRIDGE_ID_RE.fullmatch(bridge_id) else ""


def _bridge_url(creds, path):
    bridge_id = _read_bridge_id(creds)
    bridge_ip = creds["bridgeIp"]
    username = creds["username"]
    if bridge_id:
        return ("https://%s/api/%s%s" % (bridge_id, username, path),
                bridge_id, bridge_ip)
    return ("https://%s/api/%s%s" % (bridge_ip, username, path),
            None, bridge_ip)


def _read_json_capped(resp, max_bytes=MAX_RESPONSE_BYTES):
    data = resp.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise ValueError("response too large")
    return json.loads(data)


@contextlib.contextmanager
def _open_bridge_request(req, hostname, ip):
    if hostname:
        with _BridgeResolver(hostname, ip):
            with _get_opener().open(req, timeout=5) as r:
                yield r
    else:
        with _get_no_redirect_opener().open(req, timeout=5) as r:
            yield r


def _request(path, creds):
    url, hostname, ip = _bridge_url(creds, path)
    req = urllib.request.Request(url)
    with _open_bridge_request(req, hostname, ip) as r:
        return _read_json_capped(r)


def _put(creds, path, body):
    url, hostname, ip = _bridge_url(creds, path)
    req = urllib.request.Request(
        url, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"}, method="PUT")
    with _open_bridge_request(req, hostname, ip) as r:
        r.read(MAX_RESPONSE_BYTES)


_ID_RE = re.compile(r'[a-zA-Z0-9_-]{1,40}')


def main():
    if len(sys.argv) < 2:
        return
    _dispatch(sys.argv[1])


def _get_status():
    try:
        creds = _load_creds()
    except Exception:
        print(json.dumps({"paired": False}))
        return
    ble = _ble_config(creds)
    if ble is not None:
        # No bridge means no bridge certificate to verify, so bridgeId stays
        # absent here and `transport` is what stops panel.qml reading that
        # absence as "TLS verification disabled".
        print(json.dumps({"paired": True, "transport": "ble"}))
        return
    print(json.dumps({"paired": True, "bridgeId": _read_bridge_id(creds)}))


def _ble_config(creds):
    """Return the validated `ble` block from hue.json, or None for bridge mode.

    Imported lazily so the bridge path never pays for it, and so a broken
    hue_ble.py can't take the bridge transport down with it.
    """
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import hue_ble
        return hue_ble.ble_config(creds)
    except Exception:
        return None


def _dispatch_ble(op):
    """Serve a bridge-shaped reply for a bulb reached over Bluetooth LE.

    hue_ble.request() raises if the daemon is down or the bulb is
    unreachable, which the caller turns into a non-zero exit -- the same
    signal panel.qml already expects from a failed bridge call.
    """
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import hue_ble

    creds = _load_creds()
    cfg = hue_ble.ble_config(creds)

    if op == "get-scenes":
        # Scenes are read straight from the local file; no need to wake the
        # bulb to list them.
        print(json.dumps(hue_ble.scenes_payload(hue_ble.load_scenes())))
        return

    if op in ("put-light", "put-group") and len(sys.argv) >= 4:
        action = json.loads(sys.argv[3])
        if op == "put-light":
            # A single-bulb room and its one light are the same device, so a
            # light state and a group action map to the same request.
            action = dict(action)
            action.pop("scene", None)
        request = hue_ble.group_action_to_request(action)
        if request is None:
            return
        hue_ble.request(request)
        return

    state = hue_ble.request({"op": "state"})["state"]
    if op == "get-lights":
        print(json.dumps(hue_ble.lights_payload(state, cfg)))
    elif op == "get-groups":
        print(json.dumps(hue_ble.groups_payload(state, cfg)))
    elif op == "verify":
        print(1)


def _dispatch(op):
    if op == "write-favorite" and len(sys.argv) >= 4:
        _write_favorite(sys.argv[3])
        return
    if op == "write-order" and len(sys.argv) >= 4:
        _write_order(sys.argv[3])
        return
    if op == "get-status":
        _get_status()
        return

    # Every remaining op talks to the bridge. panel.qml's groupsProc/
    # scenesProc onExited only has the process exit code to tell "the
    # bridge didn't answer" apart from "the bridge genuinely has no rooms/
    # scenes" -- so a failure here has to surface as a non-zero exit, not
    # just a swallowed exception. sys.exit raises SystemExit, which isn't
    # an Exception subclass, so it passes straight through main()'s outer
    # `except Exception: pass` instead of being caught by it.
    try:
        creds = _load_creds()

        if _ble_config(creds) is not None:
            _dispatch_ble(op)
            return

        if op == "get-lights":
            print(json.dumps(_request("/lights", creds)))
        elif op == "get-groups":
            print(json.dumps(_request("/groups", creds)))
        elif op == "get-scenes":
            print(json.dumps(_request("/scenes", creds)))
        elif op == "put-light" and len(sys.argv) >= 4:
            light_id = sys.argv[2]
            if not re.fullmatch(r'[0-9]+', light_id):
                return
            state = json.loads(sys.argv[3])
            _put(creds, "/lights/%s/state" % light_id, state)
        elif op == "put-group" and len(sys.argv) >= 4:
            group_id = sys.argv[2]
            if not re.fullmatch(r'[0-9]+', group_id):
                return
            action = json.loads(sys.argv[3])
            _put(creds, "/groups/%s/action" % group_id, action)
        elif op == "verify":
            lights = _request("/lights", creds)
            print(len(lights))
    except Exception:
        sys.exit(1)


def _open_checked_dir(directory):
    # Dir-fd relative, ownership-checked open of a settings directory --
    # shared by _atomic_write (the file write itself) and _write_order's
    # lock file. Both need the same guarantee: nothing below gets used
    # unless this directory was actually created by (or already belongs
    # to) the current user.
    os.makedirs(directory, exist_ok=True)
    dir_fd = os.open(directory, os.O_DIRECTORY | os.O_NOFOLLOW)
    if os.fstat(dir_fd).st_uid != os.getuid():
        os.close(dir_fd)
        raise OSError("settings directory not owned by current user")
    # chmod via the already-validated descriptor (fchmod), not the path --
    # os.chmod(path, ...) follows symlinks by default, which would have
    # silently relocked whatever an attacker-planted symlink pointed at
    # before O_NOFOLLOW ever got a chance to reject it.
    os.chmod(dir_fd, 0o700)
    return dir_fd


def _atomic_write(path, payload, dir_fd=None):
    # dir_fd lets a caller that already holds a checked descriptor for this
    # directory (_write_order, mid-lock) reuse it instead of _atomic_write
    # re-deriving and re-validating its own -- an extra open+fstat+fchmod on
    # every write-order call otherwise, for no benefit since it's the same
    # directory checked moments earlier under the same lock.
    directory = os.path.dirname(path)
    owns_dir_fd = dir_fd is None
    if owns_dir_fd:
        dir_fd = _open_checked_dir(directory)
    try:
        fd, tmp_path = tempfile.mkstemp(dir=directory)
        tmp_name = os.path.basename(tmp_path)
        try:
            with os.fdopen(fd, 'w') as f:
                f.write(payload)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_name, os.path.basename(path),
                       src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
        except BaseException:
            try:
                os.unlink(tmp_name, dir_fd=dir_fd)
            except OSError:
                pass
            raise
    finally:
        if owns_dir_fd:
            os.close(dir_fd)


def _write_favorite(body_json):
    body = json.loads(body_json)
    if not isinstance(body, dict):
        return
    room_id = str(body.get("roomId", ""))
    if room_id and not _ID_RE.fullmatch(room_id):
        return
    config_path = os.path.join(CONFIG_HOME, "omarchy/settings/hue-favorite.json")
    _atomic_write(config_path, json.dumps({"favoriteRoomId": room_id}) + "\n")


def _valid_id_list(value):
    return isinstance(value, list) and all(
        _ID_RE.fullmatch(str(v)) for v in value)


def _valid_id_map_of_id_lists(value):
    if not isinstance(value, dict):
        return False
    for k, v in value.items():
        if not _ID_RE.fullmatch(str(k)):
            return False
        if not _valid_id_list(v):
            return False
    return True


def _valid_id_map_of_ids(value):
    if not isinstance(value, dict):
        return False
    for k, v in value.items():
        if not _ID_RE.fullmatch(str(k)):
            return False
        if not _ID_RE.fullmatch(str(v)):
            return False
    return True


LOCK_TIMEOUT_SECONDS = 5


def _acquire_lock_bounded(lock_fd, timeout=LOCK_TIMEOUT_SECONDS):
    # Non-blocking flock in a bounded retry loop rather than a plain
    # blocking LOCK_EX -- a plain blocking flock has the same shape as the
    # FIFO-hang DoS fixed elsewhere in this file: anything holding this
    # lock longer than expected (a wedged sibling process, a stray manual
    # `flock` invocation) would stall the caller, and transitively this
    # shell instance's whole action queue, forever with no escape.
    deadline = time.monotonic() + timeout
    while True:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.05)


def _write_order(body_json):
    body = json.loads(body_json)
    if not isinstance(body, dict):
        return
    if "roomOrder" in body and not _valid_id_list(body["roomOrder"]):
        return
    if "sceneOrder" in body and not _valid_id_map_of_id_lists(body["sceneOrder"]):
        return
    if "hiddenRooms" in body and not _valid_id_list(body["hiddenRooms"]):
        return
    if "lastScene" in body and not _valid_id_map_of_ids(body["lastScene"]):
        return

    directory = os.path.join(CONFIG_HOME, "omarchy/settings")
    config_path = os.path.join(directory, "hue-order.json")
    # Two bar instances (one per monitor) can each issue a write-order call
    # around the same time. _atomic_write makes the write itself atomic, but
    # without a lock spanning the read-modify-write, the second writer's
    # stale read could overwrite the first writer's update. Advisory-lock
    # a sibling file so both instances' calls serialize against each other.
    try:
        dir_fd = _open_checked_dir(directory)
    except OSError:
        return
    try:
        try:
            lock_fd = os.open(
                "hue-order.json.lock",
                os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600, dir_fd=dir_fd)
        except OSError:
            return
        try:
            st = os.fstat(lock_fd)
            if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid():
                return
            if not _acquire_lock_bounded(lock_fd):
                return
            try:
                try:
                    cfg = _read_local_json_capped(config_path, MAX_ORDER_BYTES)
                    if not isinstance(cfg, dict):
                        cfg = {}
                except Exception:
                    cfg = {}

                for key in ("roomOrder", "sceneOrder", "lastScene", "hiddenRooms"):
                    if key in body:
                        cfg[key] = body[key]

                _atomic_write(config_path, json.dumps(cfg, indent=2) + "\n", dir_fd=dir_fd)
            finally:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
        finally:
            os.close(lock_fd)
    finally:
        os.close(dir_fd)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
