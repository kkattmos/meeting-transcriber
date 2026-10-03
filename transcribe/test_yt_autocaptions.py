#!/usr/bin/env python3
"""Unit tests for yt_autocaptions — offline; yt-dlp is a stub script.

    python3 transcribe/test_yt_autocaptions.py
"""
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import yt_autocaptions as yac  # noqa: E402

# A video spoken in English whose only uploaded track is Arabic — the case
# that sent English lectures to the summarizer as Arabic in the verify run.
INFO = {
    "language": "en",
    "subtitles": {"ar": [{"ext": "json3"}], "live_chat": [{"ext": "json"}]},
    "automatic_captions": {
        "en-orig": [{"ext": "json3"}],
        "en": [{"ext": "json3"}],
        "th": [{"ext": "json3"}],   # a machine translation — never chosen
        "ar": [{"ext": "json3"}],
    },
}

JSON3 = {"events": [
    {"tStartMs": 0, "dDurationMs": 2000, "segs": [{"utf8": "hello"}, {"utf8": " world"}]},
    {"tStartMs": 2000, "aAppend": 1, "segs": [{"utf8": "\n"}]},
    {"tStartMs": 2500},
    {"tStartMs": 3000, "dDurationMs": 1500, "segs": [{"utf8": "second  line "}]},
]}


class ChooseTrackTest(unittest.TestCase):
    def test_uploaded_track_in_the_language_wins(self):
        self.assertEqual(yac.choose_track(INFO, "ar"), ("subtitles", "ar"))

    def test_spoken_language_auto_track_when_no_uploaded_match(self):
        self.assertEqual(yac.choose_track(INFO, "en"), ("automatic_captions", "en-orig"))

    def test_never_a_machine_translation(self):
        # Asking for Thai on an English video must not pick YouTube's
        # auto-translated "th" track; the spoken English is the answer.
        self.assertEqual(yac.choose_track(INFO, "th"), ("automatic_captions", "en-orig"))

    def test_auto_means_spoken(self):
        self.assertEqual(yac.choose_track(INFO, "auto"), ("automatic_captions", "en-orig"))

    def test_auto_prefers_an_uploaded_track_in_the_spoken_language(self):
        # A Khan Academy lecture, 2026-10-03: English spoken, a human "en"
        # track and two dozen translations uploaded. Not the ASR, and not
        # the alphabetically first upload ("ar").
        info = {"language": "en",
                "subtitles": {"ar": [{}], "en": [{}], "th": [{}]},
                "automatic_captions": {"en-orig": [{}], "th": [{}]}}
        self.assertEqual(yac.choose_track(info, "auto"), ("subtitles", "en"))

    def test_auto_trusts_the_detected_language_over_the_declared_one(self):
        # A Thai lecture whose uploader left the default "en".
        info = {"language": "en",
                "subtitles": {"en": [{}], "th": [{}]},
                "automatic_captions": {"th-orig": [{}], "en": [{}]}}
        self.assertEqual(yac.choose_track(info, "auto"), ("subtitles", "th"))

    def test_regional_variants_match(self):
        info = {"subtitles": {"en-GB": [{}]}}
        self.assertEqual(yac.choose_track(info, "en"), ("subtitles", "en-GB"))

    def test_old_yt_dlp_without_orig_uses_the_video_language(self):
        info = {"language": "th", "automatic_captions": {"en": [{}], "th": [{}]}}
        self.assertEqual(yac.choose_track(info, "en"), ("automatic_captions", "th"))

    def test_nothing_usable(self):
        self.assertIsNone(yac.choose_track({"subtitles": {"live_chat": [{}]}}, "en"))
        self.assertIsNone(yac.choose_track({}, "auto"))


class Json3Test(unittest.TestCase):
    def test_events_become_segments(self):
        self.assertEqual(yac.json3_to_segments(JSON3), [
            {"text": "hello world", "offset_ms": 0, "duration_ms": 2000},
            {"text": "second line", "offset_ms": 3000, "duration_ms": 1500},
        ])


class CliTest(unittest.TestCase):
    """The real CLI against a stub yt-dlp that answers -J and writes json3."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        (root / "info.json").write_text(json.dumps(INFO))
        (root / "cap.json3").write_text(json.dumps(JSON3))
        stub = root / "yt-dlp"
        stub.write_text(f"""#!/bin/sh
echo "$@" >> "{root}/calls"
case " $* " in
  *" -J "*) cat "{root}/info.json"; exit 0 ;;
esac
out=""
while [ $# -gt 0 ]; do [ "$1" = "-o" ] && out="$2"; shift; done
cp "{root}/cap.json3" "$(dirname "$out")/cap.en-orig.json3"
""")
        stub.chmod(stub.stat().st_mode | stat.S_IEXEC)
        self.root = root
        self.env = dict(os.environ, YT_DLP_BIN=str(stub))

    def tearDown(self):
        self.tmp.cleanup()

    def run_cli(self, *args):
        return subprocess.run([sys.executable, str(HERE / "yt_autocaptions.py"), *args],
                              capture_output=True, text=True, env=self.env)

    def test_prints_segments_from_the_spoken_track(self):
        r = self.run_cli("https://youtu.be/abc", "en")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(r.stdout)[0]["text"], "hello world")
        calls = (self.root / "calls").read_text()
        self.assertIn("--write-auto-subs", calls)
        self.assertIn("--sub-langs en-orig", calls)

    def test_no_track_is_exit_3(self):
        (self.root / "info.json").write_text(json.dumps({"subtitles": {}}))
        self.assertEqual(self.run_cli("https://youtu.be/abc", "en").returncode, 3)

    def test_missing_yt_dlp_is_exit_1(self):
        self.env["YT_DLP_BIN"] = str(self.root / "no-such-binary")
        self.assertEqual(self.run_cli("https://youtu.be/abc", "en").returncode, 1)


if __name__ == "__main__":
    unittest.main()
