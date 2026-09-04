#!/usr/bin/env python3
"""
Unit tests for lib/keyring.py — the numbered-env-var key ring.

Runs with no API keys, no network and no /opt: every test points the cursor
file at a temp directory and passes an explicit env dict.

    python3 lib/test_keyring.py
"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import keyring  # noqa: E402
from keyring import KeyRing, read_env_keys  # noqa: E402


class ReadEnvKeysTest(unittest.TestCase):
    def test_bare_name_is_slot_one(self):
        self.assertEqual(read_env_keys("K", env={"K": "a"}), ["a"])

    def test_numbered_slots_in_order(self):
        env = {"K_1": "a", "K_2": "b", "K_3": "c"}
        self.assertEqual(read_env_keys("K", env=env), ["a", "b", "c"])

    def test_bare_name_comes_before_numbered(self):
        env = {"K": "bare", "K_1": "one"}
        self.assertEqual(read_env_keys("K", env=env), ["bare", "one"])

    def test_a_gap_does_not_end_the_scan(self):
        # Commenting out _2 in .env must not hide _3.
        env = {"K_1": "a", "K_3": "c"}
        self.assertEqual(read_env_keys("K", env=env), ["a", "c"])

    def test_blank_and_placeholder_values_are_ignored(self):
        env = {"K_1": "", "K_2": "  ", "K_3": "changeme", "K_4": "real"}
        self.assertEqual(read_env_keys("K", env=env), ["real"])

    def test_duplicates_collapse(self):
        # The same key twice is one account; rotating between two copies would
        # look like it was spreading quota when it isn't.
        env = {"K": "a", "K_1": "a", "K_2": "b"}
        self.assertEqual(read_env_keys("K", env=env), ["a", "b"])

    def test_aliases_are_accepted(self):
        env = {"GOOGLE_API_KEY": "g", "GEMINI_API_KEY_1": "k"}
        self.assertEqual(
            read_env_keys("GEMINI_API_KEY", aliases=("GOOGLE_API_KEY",), env=env),
            ["g", "k"])

    def test_values_are_stripped(self):
        self.assertEqual(read_env_keys("K", env={"K": "  a  "}), ["a"])

    def test_no_keys_is_empty_not_an_error(self):
        self.assertEqual(read_env_keys("K", env={}), [])


class RotationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cursor = Path(self.tmp.name) / "keycursor.json"

    def tearDown(self):
        self.tmp.cleanup()

    def ring(self, keys):
        return KeyRing("K", keys, cursor_path=self.cursor)

    def test_rotate_yields_every_key_once(self):
        ring = self.ring(["a", "b", "c"])
        self.assertEqual([k for _, k in ring.rotate()], ["a", "b", "c"])

    def test_commit_advances_the_start_for_the_next_process(self):
        ring = self.ring(["a", "b", "c"])
        slot, key = next(iter(ring.rotate()))
        self.assertEqual(key, "a")
        ring.commit(slot)
        # A fresh ring — i.e. the next run — starts on the following key.
        self.assertEqual(next(iter(self.ring(["a", "b", "c"]).rotate()))[1], "b")

    def test_cursor_wraps(self):
        for expected in ("a", "b", "c", "a"):
            ring = self.ring(["a", "b", "c"])
            slot, key = next(iter(ring.rotate()))
            self.assertEqual(key, expected)
            ring.commit(slot)

    def test_rotation_starts_at_the_cursor_and_wraps_around(self):
        self.cursor.write_text(json.dumps({"K": 2}))
        ring = self.ring(["a", "b", "c"])
        self.assertEqual([k for _, k in ring.rotate()], ["c", "a", "b"])

    def test_rings_do_not_share_a_cursor(self):
        a = KeyRing("A", ["a1", "a2"], cursor_path=self.cursor)
        b = KeyRing("B", ["b1", "b2"], cursor_path=self.cursor)
        a.commit(0)
        self.assertEqual(b.start_slot(), 0)
        self.assertEqual(KeyRing("A", ["a1", "a2"],
                                 cursor_path=self.cursor).start_slot(), 1)

    def test_corrupt_cursor_file_is_survivable(self):
        self.cursor.write_text("{not json")
        ring = self.ring(["a", "b"])
        self.assertEqual(ring.start_slot(), 0)
        ring.commit(0)  # must not raise
        self.assertEqual(json.loads(self.cursor.read_text())["K"], 1)

    def test_cursor_beyond_the_key_count_is_clamped(self):
        # Dropping from three keys to two must not index out of range.
        self.cursor.write_text(json.dumps({"K": 7}))
        ring = self.ring(["a", "b"])
        self.assertEqual(ring.start_slot(), 1)

    def test_unwritable_cursor_dir_does_not_raise(self):
        ring = KeyRing("K", ["a"], cursor_path=Path("/proc/nope/cursor.json"))
        ring.commit(0)  # best-effort: a lost cursor costs one duplicate call

    def test_empty_ring_is_falsy_and_rotates_nothing(self):
        ring = self.ring([])
        self.assertFalse(ring)
        self.assertEqual(list(ring.rotate()), [])
        self.assertIsNone(ring.current())

    def test_label_never_leaks_the_key(self):
        ring = self.ring(["supersecret", "b"])
        self.assertEqual(ring.label(0), "key 1/2")
        self.assertNotIn("supersecret", ring.label(0))


class FromEnvTest(unittest.TestCase):
    def test_from_env_uses_the_cursor_path_given(self):
        with tempfile.TemporaryDirectory() as tmp:
            cursor = Path(tmp) / "c.json"
            ring = KeyRing.from_env("K", env={"K_1": "a", "K_2": "b"},
                                    cursor_path=cursor)
            self.assertEqual(len(ring), 2)
            ring.commit(0)
            self.assertTrue(cursor.is_file())

    def test_state_dir_follows_meeting_bot_root(self):
        old = os.environ.get("MEETING_BOT_ROOT")
        try:
            os.environ["MEETING_BOT_ROOT"] = "/srv/bot"
            os.environ.pop("MEETING_BOT_STATE_DIR", None)
            self.assertEqual(keyring.cursor_file(),
                             Path("/srv/bot/state/keycursor.json"))
        finally:
            if old is None:
                os.environ.pop("MEETING_BOT_ROOT", None)
            else:
                os.environ["MEETING_BOT_ROOT"] = old


class MessageTest(unittest.TestCase):
    def test_missing_keys_message_names_the_numbered_slots(self):
        msg = keyring.missing_keys_message("YT_TRANSCRIPT_KEY", 10)
        self.assertIn("YT_TRANSCRIPT_KEY_1", msg)
        self.assertIn("10", msg)
        self.assertIn(".env", msg)


if __name__ == "__main__":
    unittest.main(verbosity=2)
