#!/usr/bin/env python3
"""
Unit tests for screen/extract_frames.py: frames on change, not on a clock.

ChangeDetector is driven with hand-made hashes (the bit counts are what
matter, not real pictures); the PPM reader with bytes; and one end-to-end
case per edge (blank lead-in, audio only) with real ffmpeg on a lavfi video,
skipped when ffmpeg is missing. lib/test_media_e2e.sh runs it on a
lecture-shaped MP4 as well.
"""
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import extract_frames as ef  # noqa: E402

A = 0                        # a slide
A_CURSOR = 0b11              # the same slide, a cursor moved (2 bits)
B = (1 << 60) - 1            # a different slide (60 bits away)
C = ((1 << 60) - 1) << 200   # another one, far from both
D = ((1 << 40) - 1) << 1000  # and another
BLANK = None


def run(detector, samples, step=2.0):
    """Feed (hash, ...) at `step`-second intervals; return [(ts, kind)] saved."""
    saved = []
    for i, digest in enumerate(samples):
        kind = detector.feed(i * step, digest)
        if kind:
            saved.append((i * step, kind))
    return saved


def detector(motion=30.0, safety=300.0):
    return ef.ChangeDetector(distance=16, motion_s=motion, safety_s=safety)


class ChangeDetectorTest(unittest.TestCase):
    def test_the_first_picture_is_saved_once_it_settles(self):
        self.assertEqual(run(detector(), [A, A, A, A]), [(2.0, "scene_change")])

    def test_a_static_slide_is_saved_once_not_on_a_clock(self):
        d = detector()
        self.assertEqual(run(d, [A] * 100), [(2.0, "scene_change")])  # 200s
        self.assertIsNone(d.finish())

    def test_a_moved_cursor_is_not_a_change(self):
        self.assertEqual(run(detector(), [A, A, A_CURSOR, A_CURSOR, A]),
                         [(2.0, "scene_change")])

    def test_a_new_slide_is_saved_when_it_settles_not_mid_transition(self):
        # A, then one sample mid-fade (C), then B for good.
        self.assertEqual(run(detector(), [A, A, C, B, B, B]),
                         [(2.0, "scene_change"), (8.0, "scene_change")])

    def test_a_change_that_goes_straight_back_saves_nothing(self):
        self.assertEqual(run(detector(), [A, A, B, A, A]), [(2.0, "scene_change")])

    def test_blank_samples_are_never_saved_and_do_not_break_a_settle(self):
        self.assertEqual(run(detector(), [BLANK, BLANK, A, BLANK, A]),
                         [(8.0, "scene_change")])

    def test_an_all_blank_video_saves_nothing(self):
        d = detector()
        self.assertEqual(run(d, [BLANK] * 10), [])
        self.assertIsNone(d.finish())

    def test_a_picture_that_never_settles_gets_one_frame_per_motion_period(self):
        # 120 samples, 240s, no two within 16 bits: a 40-bit block that
        # moves 41 bits every sample, as a played video never repeats.
        moving = [((1 << 40) - 1) << (i * 41 % 4000) for i in range(120)]
        saved = run(detector(motion=30.0), moving)
        self.assertTrue(saved)
        self.assertTrue(all(kind == "motion" for _, kind in saved))
        gaps = [b[0] - a[0] for a, b in zip(saved, saved[1:])]
        self.assertTrue(all(g >= 30.0 for g in gaps), gaps)
        self.assertLessEqual(len(saved), 240 // 30)

    def test_the_safety_net_saves_after_five_quiet_minutes(self):
        saved = run(detector(safety=300.0), [A] * 200)  # 400s of one slide
        self.assertEqual(saved, [(2.0, "scene_change"), (302.0, "periodic")])

    def test_the_safety_net_can_be_turned_off(self):
        self.assertEqual(run(detector(safety=0), [A] * 200), [(2.0, "scene_change")])

    def test_a_change_in_the_last_sample_is_saved_by_finish(self):
        d = detector()
        self.assertEqual(run(d, [A, A, A, B]), [(2.0, "scene_change")])
        self.assertEqual(d.finish(), "scene_change")

    def test_a_single_sample_video_keeps_its_frame(self):
        d = detector()
        self.assertEqual(run(d, [A]), [])
        self.assertEqual(d.finish(), "scene_change")

    def test_the_default_distance_is_the_model_dedupe_distance(self):
        sys.path.insert(0, str(HERE.parent / "summarize"))
        import llm_client
        import framecrop
        self.assertEqual(llm_client.FRAME_DEDUPE_MAX_DISTANCE,
                         framecrop.SAME_SLIDE_MAX_DISTANCE)


class PpmReaderTest(unittest.TestCase):
    def test_reads_consecutive_frames_then_a_clean_end(self):
        pixels = bytes(range(2 * 3 * 3))
        stream = io.BufferedReader(io.BytesIO(
            b"P6\n2 3\n255\n" + pixels + b"P6 2 3 255\n" + pixels))
        self.assertEqual(ef._read_ppm(stream), (2, 3, pixels))
        self.assertEqual(ef._read_ppm(stream), (2, 3, pixels))
        self.assertIsNone(ef._read_ppm(stream))

    def test_a_truncated_frame_is_an_error_not_a_short_image(self):
        stream = io.BufferedReader(io.BytesIO(b"P6\n2 3\n255\n" + b"\0" * 5))
        with self.assertRaises(RuntimeError):
            ef._read_ppm(stream)

    def test_a_16_bit_frame_is_refused(self):
        stream = io.BufferedReader(io.BytesIO(b"P6\n1 1\n65535\n" + b"\0" * 6))
        with self.assertRaises(RuntimeError):
            ef._read_ppm(stream)


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"),
                     "ffmpeg is not installed")
@unittest.skipIf(ef.framecrop.Image is None, "Pillow is not installed")
class EndToEndTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def _ffmpeg(self, *args):
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", *args], check=True)

    def _extract(self, video, **env):
        out = self.dir / "frames"
        proc = subprocess.run(
            [sys.executable, str(HERE / "extract_frames.py"), str(video), str(out), "t"],
            capture_output=True, text=True,
            env={**os.environ, "FRAME_CHECK_SECONDS": "1", **env})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return json.loads((out / "manifest.json").read_text()), proc.stdout

    def test_black_lead_in_is_skipped_and_the_slide_is_saved_once(self):
        video = self.dir / "v.mp4"
        # 4s of black, then 8s of one slide with two lines of "text".
        self._ffmpeg(
            "-f", "lavfi", "-i", "color=c=0x101014:s=640x360:d=12",
            "-vf", "drawbox=x=60:y=40:w=520:h=280:color=0xf5f5f0:t=fill:enable='gte(t,4)',"
                   "drawbox=x=100:y=90:w=400:h=14:color=0x202020:t=fill:enable='gte(t,4)',"
                   "drawbox=x=100:y=160:w=300:h=14:color=0x202020:t=fill:enable='gte(t,4)'",
            "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", str(video))
        manifest, log = self._extract(video)
        kinds = [f["kind"] for f in manifest["frames"]]
        self.assertEqual(kinds, ["scene_change"], log)
        self.assertGreaterEqual(manifest["frames"][0]["timestamp_s"], 4.0)
        self.assertTrue(Path(manifest["frames"][0]["path"]).is_file())
        self.assertEqual(len(list((self.dir / "frames").glob("*.jpg"))), 1)

    def test_an_audio_only_file_gives_an_empty_manifest(self):
        audio = self.dir / "a.m4a"
        self._ffmpeg("-f", "lavfi", "-i", "sine=frequency=440:duration=3",
                     "-c:a", "aac", str(audio))
        manifest, _ = self._extract(audio)
        self.assertEqual(manifest["frames"], [])

    def test_retired_settings_are_named_not_silently_obeyed(self):
        video = self.dir / "v.mp4"
        self._ffmpeg("-f", "lavfi", "-i", "color=c=gray:s=320x180:d=2",
                     "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
                     str(video))
        _, log = self._extract(video, FRAME_PERIOD_SECONDS="60")
        self.assertIn("FRAME_PERIOD_SECONDS is no longer used", log)


if __name__ == "__main__":
    unittest.main()
