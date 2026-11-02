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
echo "inherited chain: ${SUMMARY_FALLBACK_CHAIN:-unset}"
""")
        stub.chmod(stub.stat().st_mode | stat.S_IEXEC)
        run = root / "bot" / "runs" / "Project_sync_20260927_140000"
        (run / "logs").mkdir(parents=True)
        (run / "state.json").write_text(json.dumps({
            "input": "https://meet.new", "input_type": "meeting",
            "meet_url": "https://meet.google.com/abc-defg-hij",
            "stages": {"record": {"status": "done"}}}))
        (run / "logs" / "record.log").write_text("New Google Meet: x\n")
        # .env as the server found it at start; serve.sh exported it, so
        # os.environ holds the same values — the snapshot under test.
        cls.env_file = root / ".env"
        cls.env_file.write_text("SUMMARY_FALLBACK_CHAIN=gemini\nPIPELINE_JOBS=2\n"
                                "SUMMARY_PROMPT=meeting\n")
        os.environ.update(SUMMARY_FALLBACK_CHAIN="gemini", PIPELINE_JOBS="2",
                          SUMMARY_PROMPT="meeting",
                          MEETING_BOT_ENV_FILE=str(cls.env_file))
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
                         ["drama", "lecture", "meeting", "reality", "tutorial", "video"])
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

    def test_pm2_node_ipc_variables_do_not_reach_the_children(self):
        keys = ("NODE_CHANNEL_FD", "NODE_CHANNEL_SERIALIZATION_MODE", "NODE_APP_INSTANCE")
        old = {k: os.environ.get(k) for k in keys}
        try:
            for k in keys:
                os.environ[k] = "3"
            env = self.ts.child_env()
            for k in keys:
                self.assertNotIn(k, env)
        finally:
            for k, v in old.items():
                os.environ.pop(k, None)
                if v is not None:
                    os.environ[k] = v

    def test_an_env_edit_reaches_the_next_run_without_a_restart(self):
        original = self.env_file.read_text()
        try:
            # The operator edits .env while the UI is up.
            self.env_file.write_text("SUMMARY_FALLBACK_CHAIN=claude\nPIPELINE_JOBS=3\n")
            _, opts = self.call("/api/options")
            self.assertEqual(opts["default_jobs"], "3")
            # A key .env dropped is no longer the form's default either:
            # the stale "meeting" is gone, and the built-in default shows.
            self.assertEqual(opts["default_prompt"], "video")
            status, r = self.call("/trigger", {"new_meet": True})
            self.assertEqual(status, 202)
            for _ in range(50):
                _, log = self.call("/api/log?name=" + r["log_name"])
                if "inherited chain" in log.get("text", ""):
                    break
                time.sleep(0.1)
            # The child gets no .env key at all, so its own source_env.sh
            # loads the file as it is now — not the server's stale "gemini".
            self.assertIn("inherited chain: unset", log["text"])
            env = self.ts.child_env()
            self.assertNotIn("SUMMARY_FALLBACK_CHAIN", env)
            self.assertNotIn("SUMMARY_PROMPT", env)
            self.assertEqual(env["MEETING_BOT_FOREGROUND"], "1")
            self.assertEqual(env["MEETING_BOT_TOKEN"], TOKEN)
        finally:
            self.env_file.write_text(original)

    def test_read_dotenv_follows_source_env_rules(self):
        f = Path(self.tmp.name) / "rules.env"
        f.write_text("# comment\n\nA=1\nB = \"two words\"\nC='x'\nD=~/p\nE=\nexport F=f\n")
        d = self.ts.read_dotenv(f)
        self.assertEqual(d, {"A": "1", "B": "two words", "C": "x",
                             "D": os.path.expanduser("~") + "/p", "E": "", "F": "f"})

    def test_runs_and_detail(self):
        _, r = self.call("/api/runs")
        run = r["runs"][0]
        self.assertEqual(run["meet_url"], "https://meet.google.com/abc-defg-hij")
        self.assertEqual(run["stages"]["record"], "done")
        _, d = self.call("/api/runs/" + run["run_id"])
        self.assertIn("New Google Meet", d["logs"]["record"])

    # --- 2026-11-02: the combine toggle, tags, Drive, the run history -----

    def test_combine_is_a_toggle_tags_and_drive_are_arguments(self):
        args, err = self.ts.build_args({
            "urls": ["https://youtu.be/a", "https://youtu.be/b"], "combine": True,
            "tags": "Signals, midterm", "drive_folder": "1AbCdEfGhIjKlMn",
            "drive_folder_name": "Notes", "drive_name": "Week 1"})
        self.assertIsNone(err)
        self.assertEqual(args, [
            "https://youtu.be/a", "https://youtu.be/b",
            "--drive-folder", "1AbCdEfGhIjKlMn", "--drive-folder-name", "Notes",
            "--drive-name", "Week 1", "--combine",
            "--tag", "Signals", "--tag", "midterm"])
        # Off is no argument; an old shortcut's .md path still works.
        self.assertNotIn("--combine", self.ts.build_args(
            {"url": "https://youtu.be/a", "combine": False})[0])
        self.assertEqual(self.ts.build_args(
            {"url": "https://youtu.be/a", "combine": "/x/y.md"})[0][-2:],
            ["--combine", "/x/y.md"])

    def _make_run(self, run_id, **state):
        run = self.ts.RUNS_DIR / run_id
        (run / "logs").mkdir(parents=True, exist_ok=True)
        data = {"input": "https://youtu.be/x", "input_type": "youtube",
                "stages": {"frames": {"status": "done",
                                      "artifacts": {"manifest": "/gone"}},
                           "fetch_video": {"status": "done"}}}
        data.update(state)
        (run / "state.json").write_text(json.dumps(data))
        self.addCleanup(lambda: __import__("shutil").rmtree(run, ignore_errors=True))
        return run

    def test_run_history_tags_cache_and_clear(self):
        frames = Path(self.tmp.name) / "frames"
        os.environ["FRAMES_DIR"] = str(frames)
        self.addCleanup(os.environ.pop, "FRAMES_DIR", None)
        run = self._make_run("yt_hist_20261102_100000", prompt="lecture-claude")
        (run / "video.mp4").write_bytes(b"x" * 1000)
        (run / "chunks").mkdir()
        (run / "chunks" / "k.json").write_text("{}")
        (frames / run.name).mkdir(parents=True)
        (frames / run.name / "f1.jpg").write_bytes(b"y" * 500)

        _, sz = self.call(f"/api/runs/{run.name}/sizes")
        self.assertEqual((sz["frames"], sz["media"]), (500, 1000))
        status, r = self.call(f"/api/runs/{run.name}/tags", {"tags": ["A", "a", " B "]})
        self.assertEqual((status, r["tags"]), (200, ["A", "B"]))
        _, d = self.call(f"/api/runs/{run.name}")
        self.assertEqual(d["summary"]["tags"], ["A", "B"])
        self.assertEqual(d["summary"]["prompt"], "lecture")

        status, r = self.call(f"/api/runs/{run.name}/cache", {"what": ["frames", "media"]})
        self.assertEqual(status, 200)
        self.assertEqual(r["freed"], {"frames": 500, "media": 1000})
        self.assertFalse((run / "video.mp4").exists())
        self.assertTrue((run / "chunks").exists())
        state = json.loads((run / "state.json").read_text())
        self.assertTrue(state["stages"]["frames"]["cleaned"])
        self.assertEqual(self.call(f"/api/runs/{run.name}/cache", {"what": ["nope"]})[0], 409)

        status, r = self.call(f"/api/runs/{run.name}/clear", {})
        self.assertEqual((status, r["cleared"]), (200, [run.name]))
        self.assertFalse(run.exists())

    def test_clear_refuses_a_busy_run_and_a_member_of_a_live_set(self):
        busy = self._make_run("yt_busy_20261102_100000")
        (busy / "run.lock").mkdir()
        (busy / "run.lock" / "pid").write_text(str(os.getpid()))
        self.assertEqual(self.call(f"/api/runs/{busy.name}/clear", {})[0], 409)
        self.assertTrue(busy.exists())
        member = self._make_run("yt_mem_20261102_100000",
                                combined_into="combine_1x_abc_20261102_100001")
        combine = self._make_run("combine_1x_abc_20261102_100001", input_type="combine",
                                 members=[member.name])
        self.assertEqual(self.call(f"/api/runs/{member.name}/clear", {})[0], 409)
        _, r = self.call("/api/runs/bulk", {"action": "clear",
                                            "run_ids": [combine.name, member.name],
                                            "with_members": True})
        self.assertEqual(r["results"][0]["cleared"], [combine.name, member.name])
        self.assertEqual(r["results"][1]["skipped"], "no such run")
        self.assertFalse(member.exists())

    def test_tmp_cache_and_drive_status(self):
        tmp = self.ts.BOT_ROOT / "tmp" / "rejected-answers"
        tmp.mkdir(parents=True, exist_ok=True)
        (tmp / "a.md").write_text("x" * 10)
        self.assertGreaterEqual(self.call("/api/cache")[1]["tmp"], 10)
        status, r = self.call("/api/cache/tmp", {})
        self.assertEqual(status, 200)
        self.assertFalse(tmp.exists())
        status, r = self.call("/api/drive/status")
        self.assertEqual(status, 200)
        self.assertFalse(r["logged_in"])

    def test_resume_can_name_a_drive_folder(self):
        run = self._make_run("yt_drv_20261102_100000")
        status, r = self.call(f"/api/runs/{run.name}/resume",
                              {"drive_folder": "1AbCdEfGhIjKlMn", "drive_name": "N"})
        self.assertEqual(status, 202)
        log = self.ts.LOG_DIR / r["log"]
        for _ in range(50):
            if "argv:" in log.read_text():
                break
            time.sleep(0.05)
        self.assertIn(f"argv: --run-id {run.name} --drive-folder 1AbCdEfGhIjKlMn "
                      "--drive-name N", log.read_text())

    def test_paths_from_requests_are_refused(self):
        self.assertEqual(self.call("/api/runs/..%2F..%2Fetc")[0], 404)
        self.assertEqual(self.call("/api/log?name=../../etc/passwd")[0], 400)
        self.assertEqual(self.call("/api/runs/nope/resume", {})[0], 404)


if __name__ == "__main__":
    unittest.main()
