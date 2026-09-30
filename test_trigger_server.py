#!/usr/bin/env python3
"""Tests for trigger_server.py — the web UI's API — against a stub pipeline.

A real server on a free port, in a thread; pipeline.sh is replaced by a stub
that prints its arguments, so what is under test is the mapping from a request
to a command line, the auth, and the path handling.

    python3 test_trigger_server.py
"""
import importlib
import json
import os
import stat
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
TOKEN = "test-token-for-unit-tests"


class TriggerServerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        root = Path(cls.tmp.name)
        stub = root / "pipeline.sh"
        # --dry-run answers like the real one; anything else just logs argv.
        stub.write_text("""#!/bin/sh
for a in "$@"; do [ "$a" = "--dry-run" ] && dry=1; done
if [ -n "$dry" ]; then
  case "$1" in *bad*) echo "ERROR: unrecognized input: $1" >&2; exit 1 ;; esac
  case "$*" in *extra-case*)
    printf 'ok\\tyoutube\\t-\\tnew\\t%s\\n' "$1"
    printf 'bad\\tnot recognised\\thttps://bad.example/x\\n'
    printf 'badarg\\tunusable #t= window: zz\\thttps://youtu.be/c#t=zz\\n'
    printf 'extra\\t/no/such/file.mp4\\n'; exit 0 ;;
  esac
  printf 'ok\\tyoutube\\t-\\tnew\\t%s\\n' "$1"; exit 0
fi
echo "argv: $*"
""")
        stub.chmod(stub.stat().st_mode | stat.S_IEXEC)
        run = root / "bot" / "runs" / "Project_sync_20260927_140000"
        (run / "logs").mkdir(parents=True)
        (run / "state.json").write_text(json.dumps({
            "input": "https://meet.new", "input_type": "meeting",
            "meet_url": "https://meet.google.com/abc-defg-hij",
            "stages": {"record": {"status": "done"}}}))
        (run / "logs" / "record.log").write_text("New Google Meet: x\n")
        os.environ.update(MEETING_BOT_TOKEN=TOKEN, MEETING_BOT_SCRIPT=str(stub),
                          MEETING_BOT_ROOT=str(root / "bot"), MEETING_BOT_PORT="0")
        sys.path.insert(0, str(HERE))
        cls.ts = importlib.import_module("trigger_server")
        cls.server = cls.ts.ThreadingHTTPServer(("127.0.0.1", 0), cls.ts.Handler)
        cls.server.RequestHandlerClass.log_message = lambda *a: None
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.tmp.cleanup()

    def call(self, path, body=None, token=TOKEN):
        req = urllib.request.Request(self.base + path, method="POST" if body is not None else "GET",
                                     data=json.dumps(body).encode() if body is not None else None)
        if token:
            req.add_header("Authorization", f"Bearer {token}")
        try:
            with urllib.request.urlopen(req) as res:
                return res.status, json.loads(res.read() or b"{}")
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    def test_build_args(self):
        args, err = self.ts.build_args({
            "urls": "https://youtu.be/a\n\n  https://youtu.be/b#t=1:00-2:00 ",
            "resources": "notes.md\nhttps://github.com/me/c", "clip": "5:00-",
            "combine": "/data/out/summaries/x.md", "no_combine_pdf": True, "jobs": 1})
        self.assertIsNone(err)
        self.assertEqual(args, [
            "https://youtu.be/a", "https://youtu.be/b#t=1:00-2:00",
            "--jobs", "1", "--clip", "5:00-", "--combine", "/data/out/summaries/x.md",
            "--no-combine-pdf", "--resources", "notes.md",
            "--resources", "https://github.com/me/c"])

    def test_new_meet_takes_the_name(self):
        args, _ = self.ts.build_args({"new_meet": True, "name": "Sync"})
        self.assertEqual(args, ["--new-meet", "--name", "Sync"])
        self.assertIsNotNone(self.ts.build_args({})[1])

    def test_summary_settings_and_instructions(self):
        args, err = self.ts.build_args({
            "new_meet": True, "prompt": "meeting", "summary_language": "en",
            "pdf_font": "CMU Serif",
            "instructions": "  List every decision.\nSkip the small talk.  "})
        self.assertIsNone(err)
        self.assertEqual(args, [
            "--new-meet", "--prompt", "meeting", "--summary-language", "en",
            "--pdf-font", "CMU Serif",
            "--instructions", "List every decision.\nSkip the small talk."])
        # Blank instructions are not an argument at all.
        args, _ = self.ts.build_args({"url": "https://youtu.be/a",
                                      "instructions": "  "})
        self.assertEqual(args, ["https://youtu.be/a"])

    def test_record_and_summary_source(self):
        args, err = self.ts.build_args({
            "new_meet": True, "record_media": "audio", "summary_source": "voice"})
        self.assertIsNone(err)
        self.assertEqual(args, ["--new-meet", "--summary-source", "voice",
                                "--record-media", "audio"])
        # The server passes values through; pipeline.sh is what refuses a bad
        # one (and Check shows its message).
        args, _ = self.ts.build_args({"url": "https://youtu.be/a",
                                      "summary_source": "pictures"})
        self.assertEqual(args, ["https://youtu.be/a", "--summary-source", "pictures"])

    def test_options_preselect_the_env_media_defaults(self):
        status, opt = self.call("/api/options")
        self.assertEqual(status, 200)
        self.assertEqual(opt["default_summary_source"], "both")
        self.assertEqual(opt["default_record_media"], "video")
        self.assertEqual(self.ts._choice("voice", ("both", "voice")), "voice")
        self.assertEqual(self.ts._choice("tape", ("video", "audio")), "video")

    def test_options_offer_the_prompts_and_the_fonts(self):
        status, opt = self.call("/api/options")
        self.assertEqual(status, 200)
        self.assertEqual(opt["prompts"],
                         ["lecture", "meeting", "reality", "tutorial", "video"])
        self.assertEqual(opt["summary_languages"], ["en", "th"])
        self.assertEqual(opt["fonts"]["th"], ["Bai Jamjuree", "Sarabun"])
        self.assertIn("CMU Serif", opt["fonts"]["en"])
        self.assertIn(opt["default_fonts"]["th"], opt["fonts"]["th"])

    def test_the_page_needs_no_token_but_the_api_does(self):
        with urllib.request.urlopen(self.base + "/") as res:
            self.assertIn(b"Meeting Bot", res.read())
        self.assertEqual(self.call("/api/runs", token=None)[0], 401)
        self.assertEqual(self.call("/api/runs", token="wrong")[0], 401)

    def test_check_uses_the_pipelines_own_dry_run(self):
        status, r = self.call("/api/check", {"urls": "https://youtu.be/a"})
        self.assertEqual(status, 200)
        self.assertTrue(r["ok"])
        self.assertEqual(r["plan"][0]["input"], "https://youtu.be/a")
        self.assertEqual(r["plan"][0]["status"], "ok")
        status, r = self.call("/api/check", {"urls": "https://bad.example"})
        self.assertFalse(r["ok"])
        self.assertIn("unrecognized input", r["messages"])

    def test_check_marks_every_input_the_dry_run_refused(self):
        # A `bad` line per unusable input, and `extra` for an argument that is
        # not an input at all — which the dry run itself passes (the legacy
        # name), but a form line never means.
        status, r = self.call("/api/check", {"urls": "https://youtu.be/a\nextra-case"})
        self.assertEqual(status, 200)
        self.assertFalse(r["ok"])
        self.assertEqual([p["status"] for p in r["plan"]], ["ok", "bad", "bad", "bad"])
        self.assertEqual(r["plan"][1]["reason"], "not recognised")
        self.assertEqual(r["plan"][1]["input"], "https://bad.example/x")
        self.assertFalse(r["plan"][1]["arg"])
        # As typed, so the page matches it to its line by the string.
        self.assertTrue(r["plan"][2]["arg"])
        self.assertEqual(r["plan"][2]["input"], "https://youtu.be/c#t=zz")
        self.assertTrue(r["plan"][3]["extra"] and r["plan"][3]["arg"])
        self.assertEqual(r["plan"][3]["input"], "/no/such/file.mp4")

    def test_trigger_logs_the_command(self):
        status, r = self.call("/trigger", {"new_meet": True, "name": "Sync"})
        self.assertEqual(status, 202)
        for _ in range(50):
            _, log = self.call("/api/log?name=" + r["log_name"])
            if "argv" in log.get("text", ""):
                break
            time.sleep(0.1)
        self.assertIn("argv: --new-meet --name Sync", log["text"])

    def test_runs_and_detail(self):
        _, r = self.call("/api/runs")
        run = r["runs"][0]
        self.assertEqual(run["meet_url"], "https://meet.google.com/abc-defg-hij")
        self.assertEqual(run["stages"]["record"], "done")
        _, d = self.call("/api/runs/" + run["run_id"])
        self.assertIn("New Google Meet", d["logs"]["record"])

    def test_paths_from_requests_are_refused(self):
        self.assertEqual(self.call("/api/runs/..%2F..%2Fetc")[0], 404)
        self.assertEqual(self.call("/api/log?name=../../etc/passwd")[0], 400)
        self.assertEqual(self.call("/api/runs/nope/resume", {})[0], 404)


if __name__ == "__main__":
    unittest.main()
