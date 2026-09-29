#!/usr/bin/env python3
"""Unit tests for capture.py's hosting mode (a meeting the bot created).

The browser is not involved: playwright is stubbed out before import, and the
page, the participant count and the clock are fakes. What is under test is the
decision logic — when a hosted call is ended, and when it is not — because the
failure modes there are silent: ending a call people are in, or holding an
empty one open for four hours.

    python3 screen/test_capture_host.py
"""
import os
import sys
import types
import unittest

# capture.py imports playwright at module level; the logic under test never
# touches it.
_pw = types.ModuleType("playwright")
_pw_sync = types.ModuleType("playwright.sync_api")
_pw_sync.sync_playwright = None
_pw_sync.TimeoutError = type("PWTimeout", (Exception,), {})
sys.modules.setdefault("playwright", _pw)
sys.modules.setdefault("playwright.sync_api", _pw_sync)

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import capture  # noqa: E402


class FakeClock:
    def __init__(self):
        self.now = 1_000_000.0

    def time(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class FakePage:
    def is_closed(self):
        return False

    def title(self):
        return "Meet"


class HostedMeetingTest(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.counts = []          # participant count per poll; last one repeats
        self.ended = []
        self.left = []
        self.admit_calls = 0
        self._saved = {}
        patches = {
            ("time", "time"): self.clock.time,
            ("time", "sleep"): self.clock.sleep,
            ("capture", "get_participant_count"): self._count,
            ("capture", "host_end_call"): lambda page: self.ended.append(self.clock.now),
            ("capture", "leave_meeting"): lambda page: self.left.append(self.clock.now),
            ("capture", "host_admit_waiting"): self._admit,
            ("capture", "block_screen_share_dialog"): lambda page: False,
            ("capture", "stop_unwanted_presenting"): lambda page: False,
            ("capture", "kill_requested"): lambda: False,
        }
        for (mod, name), value in patches.items():
            target = capture.time if mod == "time" else capture
            self._saved[(target, name)] = getattr(target, name)
            setattr(target, name, value)
        self._consts = (capture.NEW_MEET_WAIT_SECONDS, capture.IDLE_LEAVE_SECONDS,
                        capture.MAX_MEETING_SECONDS)
        capture.NEW_MEET_WAIT_SECONDS = 15 * 60
        capture.IDLE_LEAVE_SECONDS = 5 * 60
        capture.MAX_MEETING_SECONDS = 240 * 60

    def tearDown(self):
        for (target, name), value in self._saved.items():
            setattr(target, name, value)
        (capture.NEW_MEET_WAIT_SECONDS, capture.IDLE_LEAVE_SECONDS,
         capture.MAX_MEETING_SECONDS) = self._consts

    def _count(self, page):
        if len(self.counts) > 1:
            return self.counts.pop(0)
        return self.counts[0]

    def _admit(self, page):
        self.admit_calls += 1
        return False

    def elapsed_min(self, ts):
        return (ts - 1_000_000.0) / 60

    def test_recognises_every_spelling_of_meet_new(self):
        for url in ("meet.new", "https://meet.new", "http://meet.new/", "MEET.NEW"):
            self.assertTrue(capture.is_new_meet(url), url)
        for url in ("https://meet.google.com/abc-defg-hij", "https://meet.news/x"):
            self.assertFalse(capture.is_new_meet(url), url)

    def test_nobody_joins_ends_after_the_wait(self):
        self.counts = [1]
        capture.wait_until_meeting_ends(FakePage(), host=True)
        self.assertEqual(len(self.ended), 1)
        self.assertEqual(self.left, [], "a hosted call ends for everyone, never just leaves")
        # Not at IDLE_LEAVE's 5 minutes: an empty new call is not an idle one.
        self.assertGreaterEqual(self.elapsed_min(self.ended[0]), 15)
        self.assertLess(self.elapsed_min(self.ended[0]), 16)

    def test_knockers_are_admitted_while_waiting(self):
        self.counts = [1]
        capture.wait_until_meeting_ends(FakePage(), host=True)
        # Every few seconds, not every 15s poll.
        self.assertGreater(self.admit_calls, (15 * 60) / capture.POLL_SECONDS)

    def test_a_one_to_one_with_the_bot_is_not_idle(self):
        # Someone joins at once and stays for an hour, then leaves.
        self.counts = [2] * 240 + [1]
        capture.wait_until_meeting_ends(FakePage(), host=True)
        self.assertEqual(len(self.ended), 1)
        self.assertGreater(self.elapsed_min(self.ended[0]), 60)

    def test_ends_soon_after_everyone_leaves(self):
        self.counts = [1, 1, 4, 5, 5, 5, 1]
        capture.wait_until_meeting_ends(FakePage(), host=True)
        self.assertEqual(len(self.ended), 1)
        self.assertLess(self.elapsed_min(self.ended[0]), 3)

    def test_unreadable_count_never_ends_a_call_early(self):
        # Meet renamed its participant chip: people may well be in the call.
        self.counts = [None]
        capture.wait_until_meeting_ends(FakePage(), host=True)
        self.assertEqual(len(self.ended), 1)
        self.assertGreaterEqual(self.elapsed_min(self.ended[0]), 240,
                                "only the hard cap may end it")

    def test_joined_meeting_behaviour_is_unchanged(self):
        # Not hosting: "me + the bot" for IDLE_LEAVE is still an idle test call.
        self.counts = [2]
        capture.wait_until_meeting_ends(FakePage(), host=False)
        self.assertEqual(self.ended, [])
        self.assertEqual(len(self.left), 1)
        self.assertLess(self.elapsed_min(self.left[0]), 6)
        self.assertEqual(self.admit_calls, 0, "a guest never clicks Admit")


if __name__ == "__main__":
    unittest.main()
