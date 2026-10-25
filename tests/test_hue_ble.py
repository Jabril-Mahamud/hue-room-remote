import importlib.util
import json
import os
import socket
import tempfile
import threading
import unittest
from unittest import mock

MODULE_PATH = os.path.join(os.path.dirname(__file__), "..", "hue_ble.py")


def load_hue_ble():
    spec = importlib.util.spec_from_file_location("hue_ble_under_test", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


hue_ble = load_hue_ble()

CFG = {"address": "AA:BB:CC:DD:EE:FF", "name": "Desk", "roomClass": "Office"}


class BleConfigTests(unittest.TestCase):
    def test_returns_none_without_ble_transport(self):
        self.assertIsNone(hue_ble.ble_config({"username": "x"}))

    def test_returns_none_for_non_dict(self):
        for bad in (None, [], "ble", 3):
            self.assertIsNone(hue_ble.ble_config(bad))

    def test_rejects_malformed_address(self):
        for bad in ("", "AA:BB", "ZZ:BB:CC:DD:EE:FF", "AABBCCDDEEFF", None):
            creds = {"transport": "ble", "ble": {"address": bad}}
            self.assertIsNone(hue_ble.ble_config(creds), bad)

    def test_normalises_address_and_defaults(self):
        cfg = hue_ble.ble_config(
            {"transport": "ble", "ble": {"address": "aa:bb:cc:dd:ee:ff"}}
        )
        self.assertEqual(cfg["address"], "AA:BB:CC:DD:EE:FF")
        self.assertEqual(cfg["name"], "Hue light")
        self.assertEqual(cfg["roomClass"], "Other")

    def test_caps_name_length(self):
        cfg = hue_ble.ble_config(
            {"transport": "ble", "ble": {"address": CFG["address"], "name": "x" * 500}}
        )
        self.assertEqual(len(cfg["name"]), 200)


class PayloadTests(unittest.TestCase):
    def test_lights_payload_shape(self):
        state = {"power": True, "brightness": 120, "temp": 300, "xy": None}
        light = hue_ble.lights_payload(state, CFG)["1"]
        self.assertTrue(light["state"]["on"])
        self.assertEqual(light["state"]["bri"], 120)
        self.assertEqual(light["state"]["ct"], 300)
        self.assertNotIn("xy", light["state"])
        self.assertEqual(light["name"], "Desk")

    def test_lights_payload_clamps_brightness(self):
        for raw, expected in ((0, 1), (999, 254), (None, 254), ("nope", 254)):
            state = {"power": True, "brightness": raw}
            self.assertEqual(
                hue_ble.lights_payload(state, CFG)["1"]["state"]["bri"], expected
            )

    def test_lights_payload_drops_sentinel_xy(self):
        # The bulb reports (1.0, 1.0) in colour-temperature mode; writing that
        # back makes it reject the whole command.
        state = {"power": True, "brightness": 10, "xy": [1.0, 1.0]}
        self.assertNotIn("xy", hue_ble.lights_payload(state, CFG)["1"]["state"])

    def test_lights_payload_keeps_real_xy(self):
        state = {"power": True, "brightness": 10, "xy": [0.5, 0.4]}
        self.assertEqual(
            hue_ble.lights_payload(state, CFG)["1"]["state"]["xy"], [0.5, 0.4]
        )

    def test_groups_payload_is_a_room_holding_the_light(self):
        group = hue_ble.groups_payload({"power": False, "brightness": 50}, CFG)["1"]
        self.assertEqual(group["type"], "Room")
        self.assertEqual(group["class"], "Office")
        self.assertEqual(group["lights"], ["1"])
        self.assertFalse(group["state"]["any_on"])
        self.assertFalse(group["state"]["all_on"])
        self.assertEqual(group["action"]["bri"], 50)

    def test_scenes_payload_binds_to_the_room(self):
        scenes = hue_ble.scenes_payload({"evening": {"temp": 400}, "day": {"temp": 250}})
        self.assertEqual(sorted(scenes), ["day", "evening"])
        self.assertEqual(scenes["day"]["group"], "1")
        self.assertEqual(scenes["day"]["name"], "day")

    def test_scenes_payload_drops_invalid_entries(self):
        scenes = hue_ble.scenes_payload(
            {"ok": {"temp": 300}, "has space": {"temp": 300}, "bad": "not a dict"}
        )
        self.assertEqual(list(scenes), ["ok"])

    def test_scenes_payload_tolerates_junk(self):
        for bad in (None, [], "scenes", 7):
            self.assertEqual(hue_ble.scenes_payload(bad), {})

    def test_scenes_payload_caps_count(self):
        many = {"s%03d" % i: {"temp": 300} for i in range(hue_ble.MAX_SCENES + 50)}
        self.assertEqual(len(hue_ble.scenes_payload(many)), hue_ble.MAX_SCENES)


class GroupActionTests(unittest.TestCase):
    def test_scene_action(self):
        self.assertEqual(
            hue_ble.group_action_to_request({"scene": "evening"}),
            {"op": "scene", "name": "evening"},
        )

    def test_rejects_malformed_scene_id(self):
        self.assertIsNone(hue_ble.group_action_to_request({"scene": "../etc/passwd"}))

    def test_power_and_brightness(self):
        self.assertEqual(
            hue_ble.group_action_to_request({"on": True, "bri": 200}),
            {"op": "set", "power": True, "brightness": 200},
        )

    def test_clamps_brightness_and_temp(self):
        req = hue_ble.group_action_to_request({"bri": 9999, "ct": 10})
        self.assertEqual(req["brightness"], 254)
        self.assertEqual(req["temp"], 153)

    def test_ignores_sentinel_xy(self):
        self.assertNotIn("xy", hue_ble.group_action_to_request({"on": True, "xy": [1.0, 1.0]}))

    def test_empty_action_is_none(self):
        for bad in ({}, {"unknown": 1}, None, "on"):
            self.assertIsNone(hue_ble.group_action_to_request(bad))


class RequestTests(unittest.TestCase):
    """Exercises the socket client against a stub daemon, no BLE involved."""

    def _serve(self, reply, path):
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(path)
        server.listen(1)

        def run():
            conn, _ = server.accept()
            with conn:
                conn.recv(65536)
                conn.sendall(reply)
            server.close()

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        return thread

    def test_returns_reply_on_ok(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "s.sock")
            self._serve(b'{"ok": true, "state": {"power": true}}\n', path)
            with mock.patch.object(hue_ble, "socket_path", lambda: path):
                reply = hue_ble.request({"op": "state"})
            self.assertTrue(reply["state"]["power"])

    def test_raises_on_daemon_error(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "s.sock")
            self._serve(b'{"ok": false, "error": "light not found"}\n', path)
            with mock.patch.object(hue_ble, "socket_path", lambda: path):
                with self.assertRaises(RuntimeError):
                    hue_ble.request({"op": "state"})

    def test_raises_when_daemon_absent(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "missing.sock")
            with mock.patch.object(hue_ble, "socket_path", lambda: path):
                with self.assertRaises(OSError):
                    hue_ble.request({"op": "state"})


class ScenesFileTests(unittest.TestCase):
    def test_load_scenes_reads_file(self):
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "hue-scenes.json"), "w") as f:
                json.dump({"day": {"temp": 250}}, f)
            with mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": d}):
                self.assertEqual(hue_ble.load_scenes(), {"day": {"temp": 250}})

    def test_load_scenes_tolerates_missing_and_malformed(self):
        with tempfile.TemporaryDirectory() as d:
            with mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": d}):
                self.assertEqual(hue_ble.load_scenes(), {})
                with open(os.path.join(d, "hue-scenes.json"), "w") as f:
                    f.write("{not json")
                self.assertEqual(hue_ble.load_scenes(), {})


class SceneKindTests(unittest.TestCase):
    def test_plain_look_is_a_mood(self):
        self.assertEqual(hue_ble.scene_kind({"temp": 400, "brightness": 60}), "mood")
        self.assertEqual(hue_ble.scene_kind({"colour": "red", "effect": "none"}), "mood")

    def test_effect_scene(self):
        self.assertEqual(hue_ble.scene_kind({"colour": "orange", "effect": "fireplace"}), "effect")

    def test_one_shots_are_actions(self):
        for scene in ({"power": False}, {"flash": 5, "colour": "red"}, {"shuffle": True}):
            self.assertEqual(hue_ble.scene_kind(scene), "action")

    def test_section_defaults_to_kind(self):
        scenes = hue_ble.scenes_payload({"day": {"temp": 250}, "fire": {"effect": "candle"}})
        self.assertEqual(scenes["day"]["section"], "Scenes")
        self.assertEqual(scenes["fire"]["section"], "Effects")

    def test_explicit_section_wins(self):
        scenes = hue_ble.scenes_payload({"focus": {"temp": 180, "section": " Work "}})
        self.assertEqual(scenes["focus"]["section"], "Work")

    def test_section_rank_follows_file_order(self):
        scenes = hue_ble.scenes_payload({
            "zz": {"temp": 300, "section": "Work"},
            "aa": {"temp": 400, "section": "Evening"},
            "mm": {"temp": 200, "section": "Work"},
        })
        self.assertEqual(scenes["zz"]["sectionRank"], 0)
        self.assertEqual(scenes["mm"]["sectionRank"], 0)
        self.assertEqual(scenes["aa"]["sectionRank"], 1)


class SwatchTests(unittest.TestCase):
    def test_named_colour_scene(self):
        self.assertEqual(hue_ble.scene_swatch({"colour": "red"}), "#ff6b6b")
        self.assertEqual(hue_ble.scene_swatch({"color": "blue"}), "#7aa2ff")

    def test_temperature_scene_lerps_cool_to_warm(self):
        self.assertEqual(hue_ble.scene_swatch({"temp": 153}), "#cfe4ff")
        self.assertEqual(hue_ble.scene_swatch({"temp": 500}), "#ffb86b")
        mid = hue_ble.scene_swatch({"temp": 326})
        self.assertTrue(mid.startswith("#e7"), mid)

    def test_no_swatch_without_a_colour(self):
        for scene in ({"power": False}, {"shuffle": True}, {"xy": [0.3, 0.3]},
                      {"colour": "chartreuse"}, {"temp": "hot"}, "not a scene"):
            self.assertIsNone(hue_ble.scene_swatch(scene), scene)

    def test_payload_carries_the_swatch(self):
        payload = hue_ble.scenes_payload({
            "evening": {"colour": "orange"}, "off": {"power": False},
        })
        self.assertEqual(payload["evening"]["swatch"], "#ffa552")
        self.assertNotIn("swatch", payload["off"])


class ShuffleTests(unittest.TestCase):
    SCENES = {
        "surprise": {"shuffle": True},
        "off": {"power": False},
        "alert": {"flash": 5, "colour": "red"},
        "day": {"temp": 250},
        "cosmos": {"colour": "purple", "effect": "cosmos"},
        "bad name": {"temp": 300},
        "notascene": "nope",
    }

    def test_picks_only_mood_scenes(self):
        picks = {hue_ble.pick_shuffle(self.SCENES) for _ in range(200)}
        self.assertEqual(picks, {"day", "cosmos"})

    def test_returns_none_when_nothing_to_pick(self):
        self.assertIsNone(hue_ble.pick_shuffle({"surprise": {"shuffle": True}}))
        self.assertIsNone(hue_ble.pick_shuffle({}))


if __name__ == "__main__":
    unittest.main()
