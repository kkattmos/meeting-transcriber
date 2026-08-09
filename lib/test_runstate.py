#!/usr/bin/env python3
"""
Tests for lib/runstate.py. No network, no /opt, no API keys — everything runs
against a tmpdir, so this is safe to run anywhere.

    python3 lib/test_runstate.py

The interesting cases are the two that resume correctness depends on:
  * a `done` stage whose artifact was deleted must report `pending` again
  * two processes finishing stages concurrently must not lose either entry
"""
import json
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from runstate import DONE, FAILED, PENDING, RUNNING, STAGES, RunState  # noqa: E402

RUNSTATE_PY = str(Path(__file__).resolve().parent / "runstate.py")


class RunStateTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.run_dir = self.root / "runs" / "demo_20260809_120000"
        self.state = RunState(self.run_dir)

    def tearDown(self):
        self._tmp.cleanup()

    # --- basics -------------------------------------------------------------

    def test_init_creates_all_stages_pending(self):
        self.state.init(input="x.mp4", input_type="local_file", name="Demo")
        for stage in STAGES:
            self.assertEqual(self.state.status(stage), PENDING)
        self.assertEqual(self.state.get("input"), "x.mp4")

    def test_init_is_idempotent_and_preserves_progress(self):
        """Re-running the same command is how you resume; init must not wipe."""
        self.state.init(input="x.mp4", input_type="local_file")
        self.state.start("transcribe")
        self.state.done("transcribe")
        self.state.init(input="x.mp4", input_type="local_file")
        self.assertEqual(self.state.status("transcribe"), DONE)

    def test_lifecycle_and_attempts(self):
        self.state.init()
        self.state.start("frames")
        self.assertEqual(self.state.status("frames"), RUNNING)
        self.state.fail("frames", error="ffmpeg blew up")
        self.assertEqual(self.state.status("frames"), FAILED)
        self.state.start("frames")
        self.state.done("frames")
        self.assertEqual(self.state.status("frames"), DONE)
        self.assertEqual(self.state.stage("frames")["attempts"], 2)
        # A retry that succeeds must not leave the old error lying around.
        self.assertNotIn("error", self.state.stage("frames"))

    def test_reset_clears_stage(self):
        self.state.init()
        self.state.start("summarize")
        self.state.done("summarize")
        self.state.reset("summarize")
        self.assertEqual(self.state.status("summarize"), PENDING)
        self.assertEqual(self.state.stage("summarize")["attempts"], 0)

    def test_reset_all(self):
        self.state.init()
        for stage in ("transcribe", "frames"):
            self.state.start(stage)
            self.state.done(stage)
        self.state.reset()
        for stage in STAGES:
            self.assertEqual(self.state.status(stage), PENDING)

    # --- the resume-correctness cases --------------------------------------

    def test_done_downgrades_to_pending_when_artifact_is_gone(self):
        artifact = self.root / "transcript.txt"
        artifact.write_text("hello")
        self.state.init()
        self.state.start("transcribe")
        self.state.done("transcribe", {"txt": str(artifact)})
        self.assertEqual(self.state.status("transcribe"), DONE)

        artifact.unlink()
        # Deleting the output and re-running must regenerate it, not skip it.
        self.assertEqual(self.state.status("transcribe"), PENDING)
        # ...but the raw recorded status is still inspectable for debugging.
        self.assertEqual(self.state.status("transcribe", verify=False), DONE)

    def test_done_stays_done_when_all_artifacts_exist(self):
        txt = self.root / "a.txt"
        srt = self.root / "a.srt"
        txt.write_text("x")
        srt.write_text("y")
        self.state.init()
        self.state.done("transcribe", {"txt": str(txt), "srt": str(srt)})
        self.assertEqual(self.state.status("transcribe"), DONE)
        srt.unlink()
        self.assertEqual(self.state.status("transcribe"), PENDING)

    def test_concurrent_writes_do_not_lose_entries(self):
        """transcribe and frames finish in parallel; both must survive."""
        self.state.init()
        errors = []

        def worker(stage):
            try:
                for _ in range(25):
                    st = RunState(self.run_dir)
                    st.start(stage)
                    st.done(stage, {"out": f"/tmp/{stage}"})
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(s,))
                   for s in ("transcribe", "frames", "summarize")]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [])
        data = json.loads((self.run_dir / "state.json").read_text())
        for stage in ("transcribe", "frames", "summarize"):
            self.assertEqual(data["stages"][stage]["status"], DONE, stage)
            self.assertEqual(data["stages"][stage]["attempts"], 25, stage)

    def test_state_file_is_never_left_truncated(self):
        """Every write lands via rename, so a reader always sees valid JSON."""
        self.state.init()
        stop = threading.Event()
        bad = []

        def reader():
            while not stop.is_set():
                try:
                    json.loads((self.run_dir / "state.json").read_text())
                except json.JSONDecodeError as exc:
                    bad.append(exc)
                except OSError:
                    pass

        t = threading.Thread(target=reader)
        t.start()
        for _ in range(60):
            self.state.start("frames")
            self.state.done("frames")
        stop.set()
        t.join()
        self.assertEqual(bad, [])

    # --- CLI surface (this is what pipeline.sh actually calls) --------------

    def _cli(self, *args):
        return subprocess.run(
            [sys.executable, RUNSTATE_PY, *args],
            capture_output=True, text=True,
        )

    def test_cli_roundtrip(self):
        run_dir = str(self.run_dir)
        self._cli("init", "--run-dir", run_dir, "--input", "u", "--input-type",
                  "youtube", "--safe-name", "yt_abc")

        out = self._cli("status", "--run-dir", run_dir, "--stage", "transcribe")
        self.assertEqual(out.stdout.strip(), PENDING)

        artifact = self.root / "t.txt"
        artifact.write_text("x")
        self._cli("start", "--run-dir", run_dir, "--stage", "transcribe")
        self._cli("done", "--run-dir", run_dir, "--stage", "transcribe",
                  "--artifact", f"txt={artifact}")

        out = self._cli("status", "--run-dir", run_dir, "--stage", "transcribe")
        self.assertEqual(out.stdout.strip(), DONE)

        out = self._cli("get", "--run-dir", run_dir,
                        "--key", "stages.transcribe.artifacts.txt")
        self.assertEqual(out.stdout.strip(), str(artifact))

        # Missing key exits non-zero so `if ! runstate get ...` works in bash.
        out = self._cli("get", "--run-dir", run_dir, "--key", "stages.nope.x")
        self.assertNotEqual(out.returncode, 0)

    def test_cli_latest_picks_newest(self):
        runs_root = self.root / "runs"
        for name in ("a_20260101_000000", "b_20260202_000000"):
            self._cli("init", "--run-dir", str(runs_root / name))
        out = self._cli("latest", "--root", str(runs_root))
        self.assertEqual(out.stdout.strip(), "b_20260202_000000")

    def test_cli_latest_exits_nonzero_when_empty(self):
        out = self._cli("latest", "--root", str(self.root / "nothing-here"))
        self.assertNotEqual(out.returncode, 0)

    def test_cli_show_flags_missing_artifacts(self):
        run_dir = str(self.run_dir)
        self._cli("init", "--run-dir", run_dir)
        self._cli("done", "--run-dir", run_dir, "--stage", "summarize",
                  "--artifact", "md=/nonexistent/gone.md")
        out = self._cli("show", "--run-dir", run_dir)
        self.assertIn("[MISSING]", out.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
