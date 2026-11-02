#!/usr/bin/env python3
"""Tests for lib/gdrive.py against a fake `requests` — what we send to Drive,
and how the answers are read. No network, no Google account.

    python3 lib/test_gdrive.py
"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import gdrive  # noqa: E402


class FakeResp:
    def __init__(self, status=200, body=None, headers=None):
        self.status_code = status
        self._body = body if body is not None else {}
        self.headers = headers or {}
        self.text = json.dumps(self._body)

    def json(self):
        return self._body

    def raise_for_status(self):
        raise RuntimeError(f"HTTP {self.status_code}")


class FakeRequests:
    """Records every call; answers from a list of (match, response) rules."""

    def __init__(self, rules):
        self.rules = rules
        self.calls = []

    def _answer(self, method, url, **kw):
        self.calls.append((method, url, kw))
        for match, resp in self.rules:
            if match(method, url, kw):
                return resp(method, url, kw) if callable(resp) else resp
        raise AssertionError(f"unexpected {method} {url} {kw.get('params')}")

    def get(self, url, **kw):
        return self._answer("GET", url, **kw)

    def post(self, url, **kw):
        return self._answer("POST", url, **kw)

    def put(self, url, **kw):
        return self._answer("PUT", url, **kw)

    def request(self, method, url, **kw):
        return self._answer(method, url, **kw)


class GdriveTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        env = {"GDRIVE_TOKEN_FILE": str(Path(self.tmp.name) / "tok.json"),
               "GDRIVE_CLIENT_ID": "cid", "GDRIVE_CLIENT_SECRET": "sec",
               "BOT_GOOGLE_ACCOUNT": "Meeting.Bot@gmail.com"}
        p = mock.patch.dict(os.environ, env)
        p.start()
        self.addCleanup(p.stop)
        # The project's retry policy is not under test here.
        p = mock.patch.object(gdrive, "_with_retries", lambda f, label: f())
        p.start()
        self.addCleanup(p.stop)

    def fake(self, rules):
        fake = FakeRequests(rules)
        p = mock.patch.object(gdrive, "_requests", lambda: fake)
        p.start()
        self.addCleanup(p.stop)
        return fake

    # --- offline helpers ---------------------------------------------------

    def test_folder_ids_from_links(self):
        for value, want in (
                ("1AbCdEfGhIjKlMnOp", "1AbCdEfGhIjKlMnOp"),
                ("https://drive.google.com/drive/folders/1AbCdEfGhIjKlMnOp", "1AbCdEfGhIjKlMnOp"),
                ("https://drive.google.com/drive/u/1/folders/1AbCdEfGhIjK_-x?usp=sharing",
                 "1AbCdEfGhIjK_-x"),
                ("https://drive.google.com/open?id=1AbCdEfGhIjKlMnOp", "1AbCdEfGhIjKlMnOp"),
                ("root", "root"), ("", None), ("not a folder", None),
                ("https://example.com/", None)):
            with self.subTest(value=value):
                self.assertEqual(gdrive.parse_folder_id(value), want)

    def test_pdf_names(self):
        self.assertEqual(gdrive.pdf_name("Week 1", "x"), "Week 1.pdf")
        self.assertEqual(gdrive.pdf_name("a/b.PDF", "x"), "a-b.PDF")
        self.assertEqual(gdrive.pdf_name("  ", "run_1"), "run_1.pdf")
        self.assertEqual(gdrive.pdf_name(None, None), "summary.pdf")

    def test_query_strings_are_escaped(self):
        self.assertEqual(gdrive._q_quote("it's a \\ test"), "'it\\'s a \\\\ test'")

    def test_gmail_addresses_compare_loosely(self):
        self.assertEqual(gdrive.normalize_email("Meeting.Bot+x@googlemail.com"),
                         gdrive.normalize_email("meetingbot@gmail.com"))

    def test_status_without_a_login(self):
        s = gdrive.status()
        self.assertEqual((s["configured"], s["logged_in"]), (True, False))
        gdrive.save_token({"refresh_token": "r", "account": "someone@gmail.com"})
        self.assertEqual(oct(Path(gdrive.token_file()).stat().st_mode & 0o777), "0o600")
        s = gdrive.status()
        self.assertTrue(s["logged_in"])
        self.assertFalse(s["account_ok"])

    def test_not_logged_in_says_how_to(self):
        with self.assertRaises(gdrive.NotLoggedIn) as cm:
            gdrive.access_token()
        self.assertIn("gdrive.py login", str(cm.exception))

    def test_auth_url_asks_for_offline_drive_access_as_the_bot(self):
        url = gdrive.auth_url("cid", "http://127.0.0.1:5", "chal", "st", "bot@gmail.com")
        for part in ("access_type=offline", "prompt=consent", "code_challenge=chal",
                     "code_challenge_method=S256", "login_hint=bot%40gmail.com",
                     "scope=https%3A%2F%2Fwww.googleapis.com%2Fauth%2Fdrive&",
                     "redirect_uri=http%3A%2F%2F127.0.0.1%3A5"):
            self.assertIn(part, url)

    # --- against the fake API ----------------------------------------------

    def _token_rule(self):
        return (lambda m, u, kw: u == gdrive.TOKEN_URL,
                FakeResp(200, {"access_token": "AT"}))

    def test_a_revoked_login_is_not_logged_in(self):
        gdrive.save_token({"refresh_token": "r"})
        self.fake([(lambda m, u, kw: u == gdrive.TOKEN_URL,
                    FakeResp(400, {"error": "invalid_grant",
                                   "error_description": "Token has been revoked."}))])
        with self.assertRaises(gdrive.NotLoggedIn):
            gdrive.access_token()

    def test_upload_creates_a_new_file(self):
        gdrive.save_token({"refresh_token": "r"})
        fake = self.fake([
            self._token_rule(),
            (lambda m, u, kw: m == "GET" and u.endswith("/files"),
             FakeResp(200, {"files": []})),
            (lambda m, u, kw: m == "POST" and "/upload/" in u,
             FakeResp(200, headers={"Location": "https://up/session"})),
            (lambda m, u, kw: m == "PUT" and u == "https://up/session",
             FakeResp(200, {"id": "F1", "name": "Week 1.pdf",
                            "webViewLink": "https://drive.google.com/file/d/F1/view"})),
        ])
        pdf = Path(self.tmp.name) / "s.pdf"
        pdf.write_bytes(b"%PDF-1.4 x")
        out = gdrive.Drive().upload(pdf, "FOLDER123456", "Week 1.pdf")
        self.assertEqual((out["id"], out["replaced"]), ("F1", False))
        get = [c for c in fake.calls if c[0] == "GET"][0]
        self.assertIn("name = 'Week 1.pdf' and 'FOLDER123456' in parents",
                      get[2]["params"]["q"])
        post = [c for c in fake.calls if c[0] == "POST" and "/upload/" in c[1]][0]
        self.assertEqual(post[2]["json"], {"name": "Week 1.pdf", "parents": ["FOLDER123456"],
                                           "mimeType": "application/pdf"})
        self.assertEqual(post[2]["params"]["uploadType"], "resumable")
        self.assertEqual(post[2]["headers"]["Authorization"], "Bearer AT")
        put = [c for c in fake.calls if c[0] == "PUT"][0]
        self.assertEqual(put[2]["data"], b"%PDF-1.4 x")

    def test_upload_replaces_a_same_named_file(self):
        gdrive.save_token({"refresh_token": "r"})
        fake = self.fake([
            self._token_rule(),
            (lambda m, u, kw: m == "GET" and u.endswith("/files"),
             FakeResp(200, {"files": [{"id": "OLD", "name": "N.pdf"}]})),
            (lambda m, u, kw: m == "PATCH" and u.endswith("/files/OLD"),
             FakeResp(200, headers={"Location": "https://up/s2"})),
            (lambda m, u, kw: m == "PUT", FakeResp(200, {"id": "OLD", "name": "N.pdf"})),
        ])
        pdf = Path(self.tmp.name) / "s.pdf"
        pdf.write_bytes(b"%PDF")
        out = gdrive.Drive().upload(pdf, "FOLDER123456", "N.pdf")
        self.assertTrue(out["replaced"])
        self.assertEqual(out["webViewLink"], "https://drive.google.com/file/d/OLD/view")
        patch = [c for c in fake.calls if c[0] == "PATCH"][0]
        # A new revision keeps the file where it is: no parents in the metadata.
        self.assertEqual(patch[2]["json"], {"name": "N.pdf"})

    def test_folders_page_and_pseudo_parents(self):
        gdrive.save_token({"refresh_token": "r"})
        pages = iter([
            FakeResp(200, {"files": [{"id": "A", "name": "a",
                                      "capabilities": {"canAddChildren": True}}],
                           "nextPageToken": "p2"}),
            FakeResp(200, {"files": [{"id": "B", "name": "b", "shared": True}]}),
        ])
        fake = self.fake([
            self._token_rule(),
            (lambda m, u, kw: m == "GET" and u.endswith("/files"),
             lambda m, u, kw: next(pages)),
        ])
        drive = gdrive.Drive()
        got = drive.folders("shared")
        self.assertEqual([(f["id"], f["can_write"], f["shared"]) for f in got],
                         [("A", True, False), ("B", False, True)])
        q = [c for c in fake.calls if c[0] == "GET"][0][2]["params"]["q"]
        self.assertTrue(q.startswith("sharedWithMe and mimeType = "))
        self.assertEqual([c for c in fake.calls if c[0] == "GET"][1][2]["params"]["pageToken"],
                         "p2")

    def test_a_file_is_not_a_folder(self):
        gdrive.save_token({"refresh_token": "r"})
        self.fake([self._token_rule(),
                   (lambda m, u, kw: m == "GET",
                    FakeResp(200, {"id": "X", "name": "x.pdf",
                                   "mimeType": "application/pdf"}))])
        with self.assertRaises(gdrive.DriveError):
            gdrive.Drive().folder("XXXXXXXXXXXX")

    def test_cli_reports_errors_on_stderr(self):
        with mock.patch("sys.stdout"), mock.patch("sys.stderr"):
            self.assertEqual(gdrive.main(["parse", "nope"]), 1)
            self.assertEqual(gdrive.main(["upload", "--file", "/no/such.pdf",
                                          "--folder", "FOLDER123456"]), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
