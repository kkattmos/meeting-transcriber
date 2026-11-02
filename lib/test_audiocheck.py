#!/usr/bin/env python3
"""Tests for lib/audiocheck.py against real ffmpeg-made files.

    python3 lib/test_audiocheck.py
"""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import audiocheck  # noqa: E402


def ff(*args):
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", *args], check=True)


@unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg not installed")
class AudioCheckTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dir = tempfile.mkdtemp()
        j = lambda n: os.path.join(cls.dir, n)
        cls.tone = j("tone.m4a")
        ff("-f", "lavfi", "-i", "sine=frequency=440:duration=60", cls.tone)
        # The live failure: minutes of digital zero, a chime at each end.
        cls.chimes = j("chimes.m4a")
        ff("-f", "lavfi", "-i", "sine=frequency=880:duration=2",
           "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono:d=120",
           "-f", "lavfi", "-i", "sine=frequency=880:duration=2",
           "-filter_complex", "[0][1][2]concat=n=3:v=0:a=1", cls.chimes)
        cls.noaudio = j("noaudio.mp4")
        ff("-f", "lavfi", "-i", "color=c=black:s=64x64:d=5", cls.noaudio)
        cls.short = j("short.m4a")
        ff("-f", "lavfi", "-i", "sine=frequency=440:duration=10", cls.short)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.dir, ignore_errors=True)

    def test_sound_passes(self):
        self.assertEqual(audiocheck.main([self.tone]), 0)

    def test_a_silent_call_with_chimes_is_silent(self):
        # Its PEAK is loud — which is why the check measures duration.
        self.assertEqual(audiocheck.main([self.chimes]), 3)

    def test_no_audio_track_is_silent(self):
        self.assertEqual(audiocheck.main([self.noaudio]), 3)

    def test_a_short_clip_only_needs_to_be_mostly_sound(self):
        self.assertEqual(audiocheck.main([self.short]), 0)

    def test_an_unreadable_file_is_left_to_the_transcriber(self):
        bad = os.path.join(self.dir, "bad.mp4")
        with open(bad, "wb") as fh:
            fh.write(b"not a video")
        self.assertEqual(audiocheck.main([bad]), 2)


if __name__ == "__main__":
    unittest.main()
