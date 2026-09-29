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
        self.in_call = [True]     # is_admitted() per call; last one repeats
        self.silent_for = None    # the audio watcher: None = unknown
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
            ("capture", "meeting_audio_silent_for"): lambda: self.silent_for,
            ("capture", "is_admitted"): lambda page: self.in_call.pop(0) if len(self.in_call) > 1 else self.in_call[0],
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

    def test_the_self_view_is_minimised_only_with_company(self):
        # Alone, the bot's tile is the stage and has no Minimize; trying
        # then only put a menu in the recording and backed off five minutes.
        calls = []

        class MeetPage(FakePage):
            url = "https://meet.google.com/abc-defg-hij"

        saved = (capture.minimize_self_tile, capture.dismiss_notices)
        capture.minimize_self_tile = lambda page: calls.append(self.clock.now)
        capture.dismiss_notices = lambda page: []
        capture._SELF_TILE_NEXT_TRY = 999e12
        try:
            self.counts = [1] * 10 + [2] * 10 + [1]
            capture.wait_until_meeting_ends(MeetPage(), host=True)
        finally:
            capture.minimize_self_tile, capture.dismiss_notices = saved
        self.assertTrue(calls, "never tried once someone was there")
        first_guest_poll = 1_000_000.0 + 10 * capture.POLL_SECONDS
        self.assertGreater(calls[0], first_guest_poll - 1,
                           "tried while the bot was alone")
        # And the back-off was lifted the moment the guest arrived.
        self.assertEqual(capture._SELF_TILE_NEXT_TRY, 0.0)

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

    def test_dropping_out_of_the_call_ends_the_recording(self):
        # Live 2026-09-29: the bot fell back to the pre-join page and the
        # recorder filmed an empty lobby. Two polls without the in-call
        # controls end it — with nothing to click, since there is no call.
        self.counts = [3]
        # is_admitted is asked on each poll, and once more by leave().
        self.in_call = [True, True, False, False]
        capture.wait_until_meeting_ends(FakePage(), host=True)
        self.assertEqual(self.ended, [], "no Leave/End clicks outside a call")
        self.assertLess(self.elapsed_min(self.clock.now), 2)

    def test_a_single_missed_reading_is_not_the_end(self):
        # One poll without the controls (a re-render) and it's back: the call
        # goes on until the hard cap, which then ends it the normal way.
        self.counts = [3]
        self.in_call = [True, False, True]
        capture.MAX_MEETING_SECONDS = 5 * 60
        capture.wait_until_meeting_ends(FakePage(), host=True)
        self.assertEqual(len(self.ended), 1, "ended by the cap, not by one bad poll")
        self.assertGreaterEqual(self.elapsed_min(self.ended[0]), 5)

    def test_a_talking_one_to_one_is_not_idle(self):
        # Joining someone's 1:1: two people, audio live — stays until the cap.
        self.counts = [2]
        self.silent_for = 0
        capture.MAX_MEETING_SECONDS = 30 * 60
        capture.wait_until_meeting_ends(FakePage(), host=False)
        self.assertEqual(len(self.left), 1)
        self.assertGreaterEqual(self.elapsed_min(self.left[0]), 30)

    def test_a_silent_one_to_one_is_idle(self):
        self.counts = [2]
        self.silent_for = 300
        capture.wait_until_meeting_ends(FakePage(), host=False)
        self.assertLess(self.elapsed_min(self.left[0]), 6)

    def test_most_people_left_but_the_lecturer_is_talking(self):
        # Peak 40, twelve stay for questions: not the end while there's sound.
        self.counts = [40, 40, 12]
        self.silent_for = 0
        capture.MAX_MEETING_SECONDS = 30 * 60
        capture.wait_until_meeting_ends(FakePage(), host=False)
        self.assertGreaterEqual(self.elapsed_min(self.left[0]), 30)

    def test_most_people_left_and_it_went_quiet(self):
        self.counts = [40, 40, 12]
        self.silent_for = 180
        capture.wait_until_meeting_ends(FakePage(), host=False)
        self.assertLess(self.elapsed_min(self.left[0]), 2)

    def test_alone_ends_even_with_sound(self):
        # Everyone left: the bot's own page may still be making noise.
        self.counts = [3, 1]
        self.silent_for = 0
        capture.wait_until_meeting_ends(FakePage(), host=True)
        self.assertEqual(len(self.ended), 1)
        self.assertLess(self.elapsed_min(self.ended[0]), 2)


class SelfTileChoiceTest(unittest.TestCase):
    """Which "More options for …" menu is the bot's own. The first live
    guest join (2026-09-29) opened the operator's tile menu, because the
    first in DOM order was taken; the bot's floating tile sits bottom-right."""

    OTHER = {"label": "ตัวเลือกเพิ่มเติมสำหรับ Khattiya", "self": False, "corner": 400}
    SELF = {"label": "ตัวเลือกเพิ่มเติมสำหรับ Meeting transcriber", "self": False,
            "corner": 2300}
    PRES = {"label": "ตัวเลือกเพิ่มเติมสำหรับ การนำเสนอ", "self": False, "corner": 900}

    def test_nearest_the_bottom_right_first(self):
        order = capture._self_menu_order([self.OTHER, self.PRES, self.SELF])
        self.assertEqual(order[0], self.SELF["label"])
        self.assertEqual(len(order), 3)

    def test_the_doms_self_marker_wins(self):
        marked = dict(self.OTHER, self=True)
        self.assertEqual(capture._self_menu_order([marked, self.SELF]),
                         [marked["label"]])

    def test_a_remembered_label_is_the_only_one_tried(self):
        self.assertEqual(
            capture._self_menu_order([self.OTHER, self.SELF], self.OTHER["label"]),
            [self.OTHER["label"]])

    def test_probes_are_bounded(self):
        many = [{"label": f"More options for P{i}", "corner": i} for i in range(9)]
        self.assertEqual(len(capture._self_menu_order(many)),
                         capture._SELF_MENU_MAX_PROBES)
        self.assertEqual(capture._self_menu_order([]), [])


if __name__ == "__main__":
    unittest.main()
