#!/usr/bin/env python3
"""Unit tests for screen/browser.py — no browser, no network.

The live half (Firefox ESR actually launching, the adapter against a real
page, ListAccounts answering) is exercised by verify_e2e.sh --browser-smoke
and `python3 screen/browser.py check-account`.
"""
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import browser  # noqa: E402

BOT = "the.bot.account@gmail.com"  # a placeholder, not the real account


class BrowserChoiceTest(unittest.TestCase):
    def test_firefox_esr_is_the_default(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("MEETING_BROWSER", None)
            self.assertEqual(browser.browser_kind(), "firefox")

    def test_aliases(self):
        for v in ("firefox", "firefox-esr", "FIREFOX-ESR", "esr"):
            self.assertEqual(browser.browser_kind(v), "firefox", v)
        for v in ("chrome", "google-chrome-stable"):
            self.assertEqual(browser.browser_kind(v), "chrome", v)

    def test_unknown_browser_is_refused(self):
        with self.assertRaises(SystemExit):
            browser.browser_kind("chromium")

    def test_each_browser_has_its_own_profile(self):
        env = {"MEETING_BOT_ROOT": "/r"}
        with mock.patch.dict(os.environ, env):
            os.environ.pop("CHROME_PROFILE_DIR", None)
            os.environ.pop("FIREFOX_PROFILE_DIR", None)
            self.assertNotEqual(browser.profile_dir("chrome"),
                                browser.profile_dir("firefox"))

    def test_mic_and_camera_are_blocked(self):
        # The bot only listens. A PC has a webcam and a microphone; neither
        # may ever reach Meet, and a blocked device can't be clicked on.
        self.assertIn("--deny-permission-prompts", browser.CHROME_ARGS)
        self.assertNotIn("--use-fake-ui-for-media-stream", browser.CHROME_ARGS)
        self.assertEqual(browser.FIREFOX_PREFS["permissions.default.camera"], 2)
        self.assertEqual(browser.FIREFOX_PREFS["permissions.default.microphone"], 2)
        # This pref would skip the check and GRANT access.
        self.assertFalse(browser.FIREFOX_PREFS["media.navigator.permission.disabled"])

    def test_chrome_sandbox_stays_on_unless_root(self):
        # --no-sandbox is added at launch for root only.
        self.assertNotIn("--no-sandbox", browser.CHROME_ARGS)

    def test_screen_share_layer_one_in_both(self):
        self.assertIn("--disable-features=ScreenCapture", browser.CHROME_ARGS)
        self.assertFalse(browser.FIREFOX_PREFS["media.getdisplaymedia.enabled"])

    def test_a_live_lock_is_refused_not_deleted(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            os.symlink(f"127.0.1.1:+{os.getppid()}", os.path.join(d, "lock"))
            with self.assertRaises(browser.ProfileInUse):
                browser.clear_stale_locks("firefox", d)
            self.assertTrue(os.path.lexists(os.path.join(d, "lock")))

    def test_stale_firefox_lock_is_cleared(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            os.symlink("host:1234", os.path.join(d, "lock"))
            Path(d, ".parentlock").touch()
            Path(d, "prefs.js").touch()
            browser.clear_stale_locks("firefox", d)
            self.assertEqual(os.listdir(d), ["prefs.js"])


class AccountTest(unittest.TestCase):
    def test_parse_list_accounts_json(self):
        body = ('["gaia.l.a.r",[["gaia.l.a",1,"Bot","%s",'
                '"https://x/photo.jpg",1,1,0,null,1,"123",null,null,null,null,1]]]' % BOT)
        self.assertEqual(browser.parse_accounts(body), [BOT])

    def test_signed_out_is_an_empty_list_not_none(self):
        self.assertEqual(browser.parse_accounts('["gaia.l.a.r",[]]'), [])

    def test_an_error_page_is_unknown_not_signed_out(self):
        self.assertIsNone(browser.parse_accounts("<html>400. That's an error.</html>"))

    def test_gmail_dots_and_case_are_the_same_account(self):
        self.assertEqual(browser.normalize_email("The.Bot.Account@Gmail.com"),
                         browser.normalize_email(BOT))
        self.assertNotEqual(browser.normalize_email("a.b@example.com"),
                            browser.normalize_email("ab@example.com"))

    def test_verdicts(self):
        v = browser.account_verdict
        self.assertEqual(v([BOT], BOT)[0], "ok")
        self.assertEqual(v(["someone@gmail.com", BOT], BOT)[0], "ok")
        self.assertEqual(v(["someone@gmail.com"], BOT)[0], "wrong")
        self.assertEqual(v([], BOT)[0], "signed-out")
        self.assertEqual(v(None, BOT)[0], "unchecked")
        self.assertEqual(v(["someone@gmail.com"], None)[0], "unchecked")

    def test_the_account_is_not_hardcoded(self):
        import re
        src = Path(browser.__file__).read_text()
        self.assertEqual(re.findall(r"[\w.+-]+@[\w-]+\.[\w.]+", src), [],
                         "an email address is written into browser.py")
        with mock.patch.dict(os.environ, {"BOT_GOOGLE_ACCOUNT": " x@y.z "}):
            self.assertEqual(browser.expected_account(), "x@y.z")
        with mock.patch.dict(os.environ, {"BOT_GOOGLE_ACCOUNT": ""}):
            self.assertIsNone(browser.expected_account())

    def test_authuser_steers_meet_only(self):
        w = browser.with_authuser
        self.assertEqual(w("https://meet.google.com/abc-defg-hij", BOT),
                         f"https://meet.google.com/abc-defg-hij?authuser={BOT}")
        self.assertEqual(w("https://meet.google.com/abc-defg-hij?hs=1", BOT),
                         f"https://meet.google.com/abc-defg-hij?hs=1&authuser={BOT}")
        self.assertEqual(w("https://zoom.us/j/1", BOT), "https://zoom.us/j/1")
        self.assertEqual(w("https://meet.google.com/abc-defg-hij", None),
                         "https://meet.google.com/abc-defg-hij")


class CaptureAccountGateTest(unittest.TestCase):
    """capture.ensure_bot_account: refuse the wrong account, allow unknown."""

    def setUp(self):
        import capture
        self.capture = capture

    def _run(self, accounts, expected):
        page = mock.Mock()
        with mock.patch.object(browser, "read_signed_in_accounts", return_value=accounts):
            return self.capture.ensure_bot_account(page, expected)

    def test_gate(self):
        self.assertTrue(self._run([BOT], BOT))
        self.assertFalse(self._run(["other@gmail.com"], BOT))
        self.assertFalse(self._run([], BOT))
        self.assertTrue(self._run(None, BOT))
        self.assertTrue(self._run(None, None))


if __name__ == "__main__":
    unittest.main()
