#!/usr/bin/env python3
"""The Zoom join and leave decisions, on a fake page (no browser, no network)."""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import capture  # noqa: E402


class Btn:
    def __init__(self, page, name):
        self.page, self.name = page, name

    def is_visible(self, timeout=None):
        return self.name in self.page.visible

    def click(self):
        self.page.clicked.append(self.name)
        self.page.on_click(self.name)


class Page:
    def __init__(self, text="", visible=(), form=None):
        self.text, self.visible, self.form = text, set(visible), form
        self.clicked, self.goto_url = [], None
        self.confirm = None

    def on_click(self, name):
        pass

    def get_by_role(self, role, name=None, exact=False):
        matches = [v for v in self.visible
                   if v == name or (not exact and name.lower() in v.lower())]
        return Btn(self, matches[0] if matches else "\0absent")

    def inner_text(self, sel):
        return self.text

    def goto(self, url, wait_until=None):
        self.goto_url = url

    def wait_for_timeout(self, ms):
        pass

    def evaluate(self, js):
        return self.form if self.form is not None else self.confirm


class ZoomJoinTest(unittest.TestCase):
    def test_a_pwd_link_is_kept_and_joined(self):
        page = Page(visible={"Join"}, form={"name": "Bot", "passcode_empty": None})
        self.assertTrue(capture.join_zoom(page, "https://x.zoom.us/j/1?pwd=a", "Bot"))
        self.assertEqual(page.goto_url, "https://x.zoom.us/wc/join/1?pwd=a")
        self.assertEqual(page.clicked, ["Join"])

    def test_it_joins_muted(self):
        page = Page(visible={"Mute", "Join"}, form={"name": "Bot", "passcode_empty": None})
        capture.join_zoom(page, "https://x.zoom.us/j/1?pwd=a", "Bot")
        self.assertEqual(page.clicked, ["Mute", "Join"])

    def test_an_already_muted_preview_is_left_alone(self):
        page = Page(visible={"Unmute", "Join"}, form={"name": "Bot", "passcode_empty": None})
        capture.join_zoom(page, "https://x.zoom.us/j/1?pwd=a", "Bot")
        self.assertEqual(page.clicked, ["Join"])

    def test_an_empty_passcode_box_is_never_joined(self):
        page = Page(visible={"Join"}, form={"name": "Bot", "passcode_empty": True})
        self.assertFalse(capture.join_zoom(page, "https://x.zoom.us/j/1", "Bot"))
        self.assertEqual(page.clicked, [])

    def test_the_passcode_form_is_a_refusal(self):
        reason = capture.join_rejection_reason(
            Page(text="Enter Meeting Info Meeting Passcode Your Name Join"))
        self.assertIn("passcode", reason)

    def test_zooms_bot_check_is_a_refusal(self):
        reason = capture.join_rejection_reason(
            Page(text="Automated bots aren't allowed to join this meeting."))
        self.assertIn("bot check", reason)

    def test_a_joinable_form_is_not_a_refusal(self):
        self.assertIsNone(capture.join_rejection_reason(
            Page(text="Enter Meeting Info Your Name Join")))


class AdmittedTest(unittest.TestCase):
    def test_the_prejoin_footer_is_not_a_call(self):
        # "End" as a substring of "Send Report" filmed a form as the meeting.
        self.assertFalse(capture.is_admitted(
            Page(visible={"Send Report", "Mute", "Stop Video"})))

    def test_the_call_toolbar_is(self):
        self.assertTrue(capture.is_admitted(Page(visible={"Leave", "audio"})))


class LeaveTest(unittest.TestCase):
    def leave(self, page):
        clock = iter(range(0, 1000))
        with mock.patch.object(capture.time, "sleep"), \
                mock.patch.object(capture.time, "time", lambda: next(clock)), \
                mock.patch("builtins.print") as out:
            capture.leave_meeting(page)
        return " ".join(str(c.args[0]) for c in out.call_args_list if c.args)

    def test_reports_leaving_once_the_controls_go(self):
        page = Page(visible={"Leave"})
        page.on_click = lambda name: page.visible.clear()
        self.assertIn("Left the meeting", self.leave(page))

    def test_zooms_red_button_does_the_leaving(self):
        page = Page(visible={"Leave"})

        def click(name):
            if name == "Leave":
                page.visible.add("Leave Meeting")
            elif name == "Leave Meeting":
                page.visible.clear()
        page.on_click = click
        self.assertIn("Left the meeting", self.leave(page))
        self.assertEqual(page.clicked, ["Leave", "Leave Meeting"])

    def test_warns_when_still_in_the_call(self):
        self.assertIn("still in the call", self.leave(Page(visible={"Leave"})))


if __name__ == "__main__":
    unittest.main()
