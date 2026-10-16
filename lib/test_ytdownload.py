#!/usr/bin/env python3
"""Unit tests for lib/ytdownload.py — a stub yt-dlp, no network, no sleeping.

    python3 lib/test_ytdownload.py
"""
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import ytdownload  # noqa: E402

# Fails with the stderr in $STUB_ERR while $STUB_COUNT > 0 (decremented per
# call), then writes the -o file. Every call's argv is appended to $STUB_LOG.
STUB = r"""#!/bin/bash
echo "$*" >> "$STUB_LOG"
n=$(cat "$STUB_COUNT" 2>/dev/null || echo 0)
if [ "$n" -gt 0 ]; then
  echo "$((n - 1))" > "$STUB_COUNT"
  echo "$STUB_ERR" >&2; exit 1
fi
out=""; while [ "$#" -gt 0 ]; do case "$1" in -o) out="$2"; shift 2;; *) shift;; esac; done
echo video > "${out/%.%(ext)s/.mp4}"
"""

REFUSED = "ERROR: unable to download video data: HTTP Error 403: Forbidden"


class Classification(unittest.TestCase):
    def test_transient_failures(self):
        for err in (REFUSED,
                    "ERROR: unable to download video data: HTTP Error 429: Too Many Requests",
                    "ERROR: HTTP Error 503: Service Unavailable",
                    "ERROR: ... The read operation timed out",
                    "ERROR: ... Connection reset by peer",
                    "ERROR: ... IncompleteRead(1024 bytes read)"):
            self.assertTrue(ytdownload.is_transient(err), err)

    def test_permanent_failures_are_never_retried(self):
        for err in ("ERROR: [youtube] abc: Video unavailable",
                    "ERROR: [youtube] abc: Private video. Sign in if you've been granted access",
                    "ERROR: [youtube] abc: Sign in to confirm you're not a bot",
                    "ERROR: [youtube] abc: Requested format is not available",
                    # A permanent marker wins over a transient-looking one.
                    "HTTP Error 403: Forbidden\nERROR: Video unavailable"):
            self.assertFalse(ytdownload.is_transient(err), err)

    def test_an_unknown_failure_is_not_retried(self):
        self.assertFalse(ytdownload.is_transient("ERROR: something new"))


class Download(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        stub = self.tmp / "yt-dlp"
        stub.write_text(STUB)
        stub.chmod(stub.stat().st_mode | stat.S_IEXEC)
        self.log = self.tmp / "calls"
        self.count = self.tmp / "count"
        self.env = mock.patch.dict(os.environ, {
            "YT_DLP_BIN": str(stub), "STUB_LOG": str(self.log),
            "STUB_COUNT": str(self.count), "STUB_ERR": REFUSED,
            "YT_DOWNLOAD_RETRY_SECONDS": "30"})
        self.env.start()
        self.sleeps = []
        self.sleep = mock.patch.object(ytdownload, "_sleep", self.sleeps.append)
        self.sleep.start()
        self.messages = []

    def tearDown(self):
        self.sleep.stop()
        self.env.stop()

    def run_download(self, **env):
        with mock.patch.dict(os.environ, env):
            return ytdownload.download(
                "https://www.youtube.com/watch?v=abcdefghijk",
                str(self.tmp / "video.%(ext)s"), quiet=True,
                log=self.messages.append)

    def calls(self):
        return self.log.read_text().splitlines() if self.log.exists() else []

    def test_first_attempt_succeeds(self):
        rc, _ = self.run_download()
        self.assertEqual(rc, 0)
        self.assertEqual(len(self.calls()), 1)
        self.assertEqual(self.sleeps, [])

    def test_403_twice_then_served(self):
        self.count.write_text("2")
        rc, _ = self.run_download()
        self.assertEqual(rc, 0)
        self.assertTrue((self.tmp / "video.mp4").exists())
        self.assertEqual(len(self.calls()), 3)
        self.assertTrue(any("attempt 3 of 4" in m for m in self.messages))

    def test_waits_double_with_bounded_jitter(self):
        self.count.write_text("9")
        rc, _ = self.run_download()
        self.assertNotEqual(rc, 0)
        self.assertEqual(len(self.calls()), 4)
        self.assertEqual(len(self.sleeps), 3)
        for wait, base in zip(self.sleeps, (30, 60, 120)):
            self.assertGreaterEqual(wait, base)
            self.assertLessEqual(wait, base * 1.25)
        self.assertTrue(any("after 4 attempts" in m for m in self.messages))

    def test_attempts_are_configurable(self):
        self.count.write_text("9")
        self.run_download(YT_DOWNLOAD_ATTEMPTS="2")
        self.assertEqual(len(self.calls()), 2)

    def test_a_permanent_failure_runs_once(self):
        self.count.write_text("9")
        rc, err = self.run_download(STUB_ERR="ERROR: [youtube] x: Video unavailable")
        self.assertNotEqual(rc, 0)
        self.assertIn("Video unavailable", err)
        self.assertEqual(len(self.calls()), 1)
        self.assertEqual(self.sleeps, [])

    def test_the_command_never_merges(self):
        self.run_download()
        argv = self.calls()[0]
        self.assertIn("--no-playlist", argv)
        self.assertIn(ytdownload.FORMAT, argv)
        self.assertNotIn("bestaudio", ytdownload.FORMAT)
        self.assertNotIn("merge-output-format", argv)


if __name__ == "__main__":
    unittest.main()
