#!/usr/bin/env python3
"""Unit tests for lib/clip.py — no ffmpeg, no network.

The ffmpeg invocation is checked by capturing the argv rather than by running
it: what matters about `cut` is the flag order (-ss before -i, -t not -to) and
`-avoid_negative_ts make_zero`, and those are assertions about the command, not
about the bytes that come out.

    python3 lib/test_clip.py
"""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import clip  # noqa: E402


class ParseTime(unittest.TestCase):
    def test_the_three_accepted_spellings(self):
        self.assertEqual(clip.parse_time("01:30:00"), 5400.0)
        self.assertEqual(clip.parse_time("90:00"), 5400.0)
        self.assertEqual(clip.parse_time("5400"), 5400.0)

    def test_fractional_seconds_survive(self):
        self.assertAlmostEqual(clip.parse_time("00:00:01.250"), 1.25)

    def test_minutes_may_exceed_sixty(self):
        # "90:00" is a perfectly ordinary way to say an hour and a half, and
        # rejecting it would only teach the operator to do arithmetic.
        self.assertEqual(clip.parse_time("90:00"), 5400.0)

    def test_junk_is_rejected(self):
        for bad in ("", "abc", "1:2:3:4", "-5", "1,30"):
            with self.assertRaises(clip.ClipError):
                clip.parse_time(bad)


class ParseClip(unittest.TestCase):
    def test_a_plain_window(self):
        self.assertEqual(clip.parse_clip("00:05:00-01:30:00"), (300.0, 5400.0))

    def test_open_end_runs_to_the_end_of_the_video(self):
        self.assertEqual(clip.parse_clip("00:05:00-"), (300.0, None))
        self.assertEqual(clip.parse_clip("00:05:00"), (300.0, None))

    def test_open_start_begins_at_zero(self):
        self.assertEqual(clip.parse_clip("-00:10:00"), (0.0, 600.0))

    def test_pasted_en_dash_and_the_word_to(self):
        # Both come straight out of a document or a chat message, and neither
        # is worth an error the operator has to decode.
        self.assertEqual(clip.parse_clip("00:05:00–01:30:00"), (300.0, 5400.0))
        self.assertEqual(clip.parse_clip("00:05:00 to 01:30:00"), (300.0, 5400.0))

    def test_a_backwards_window_is_refused(self):
        # Not clamped, not swapped: silently summarizing a different window
        # than the one asked for is the failure this whole module exists to
        # avoid, and it would only be noticed by reading the output.
        with self.assertRaises(clip.ClipError):
            clip.parse_clip("01:30:00-00:05:00")
        with self.assertRaises(clip.ClipError):
            clip.parse_clip("00:05:00-00:05:00")

    def test_the_whole_video_is_refused(self):
        # "0-" is not a window, and accepting it would create a second run id
        # for a run identical to the unclipped one.
        with self.assertRaises(clip.ClipError):
            clip.parse_clip("0-")


class Naming(unittest.TestCase):
    def test_label_is_canonical(self):
        # Every spelling of the same window has to produce ONE label, because
        # the label is what runstate matches on for auto-resume: "5:00-90:00"
        # and "00:05:00-01:30:00" must resume the same run rather than making
        # two runs of the same 85 minutes.
        for spelling in ("00:05:00-01:30:00", "5:00-90:00", "300-5400",
                         "00:05:00 to 01:30:00"):
            self.assertEqual(clip.label(clip.parse_clip(spelling)),
                             "00:05:00-01:30:00")

    def test_token_is_filename_safe_and_distinct(self):
        # It becomes part of the run id, and every artifact path derives from
        # the run id — so it may not contain a colon, a slash or a space.
        token = clip.token(clip.parse_clip("00:05:00-01:30:00"))
        self.assertEqual(token, "c000500-013000")
        self.assertFalse(set(token) & set(":/ "))
        self.assertNotEqual(token, clip.token(clip.parse_clip("01:30:00-02:00:00")))

    def test_every_label_parses_back_to_its_own_window(self):
        # The label is not decoration: it is what pipeline.sh writes into
        # state.json and what the clip stage parses back on every attempt,
        # including every resume. A label that does not round-trip fails after
        # the download rather than at the command line — which is how the
        # open-end spelling ("00:05:00-end") was caught.
        for spelling in ("00:05:00-01:30:00", "00:05:00-", "-00:10:00",
                         "5:00-90:00", "300-5400", "0:30-45:00"):
            window = clip.parse_clip(spelling)
            self.assertEqual(clip.parse_clip(clip.label(window)), window,
                             f"{spelling} -> {clip.label(window)} does not round-trip")

    def test_open_end_token(self):
        self.assertEqual(clip.token(clip.parse_clip("00:05:00-")), "c000500-end")


class WindowSegments(unittest.TestCase):
    """The caption path: no media to cut, so the segments are windowed instead."""

    def seg(self, offset_ms, duration_ms, text="x"):
        return {"text": text, "offset_ms": offset_ms, "duration_ms": duration_ms}

    def test_segments_outside_the_window_are_dropped(self):
        segments = [self.seg(0, 1000, "before"),
                    self.seg(400_000, 1000, "inside"),
                    self.seg(9_000_000, 1000, "after")]
        kept = clip.window_segments(segments, (300.0, 5400.0))
        self.assertEqual([s["text"] for s in kept], ["inside"])

    def test_kept_segments_are_rebased_to_the_clip(self):
        # This is the half that makes the caption path agree with a cut media
        # file. If it only filtered, the .srt would start at 0:05:00 while the
        # frames extracted from the clip started at 0:00:00, and every frame
        # would be assigned to the wrong words.
        kept = clip.window_segments([self.seg(400_000, 2000)], (300.0, 5400.0))
        self.assertEqual(kept[0]["offset_ms"], 100_000)
        self.assertEqual(kept[0]["duration_ms"], 2000)

    def test_a_straddling_segment_is_truncated_not_dropped(self):
        # A caption cue that began two seconds before the window still carries
        # words spoken inside it. Dropping it loses the opening sentence of the
        # clip; keeping it whole would give it a negative offset.
        kept = clip.window_segments([self.seg(298_000, 10_000, "straddles")],
                                    (300.0, 5400.0))
        self.assertEqual(len(kept), 1)
        self.assertEqual(kept[0]["offset_ms"], 0)
        self.assertEqual(kept[0]["duration_ms"], 8000)

    def test_the_tail_is_truncated_at_the_end_of_the_window(self):
        kept = clip.window_segments([self.seg(5_399_000, 10_000)], (300.0, 5400.0))
        self.assertEqual(kept[0]["duration_ms"], 1000)

    def test_an_open_end_keeps_everything_after_the_start(self):
        segments = [self.seg(0, 1000), self.seg(400_000, 1000),
                    self.seg(9_000_000, 1000)]
        kept = clip.window_segments(segments, (300.0, None))
        self.assertEqual(len(kept), 2)

    def test_other_fields_are_carried_through(self):
        segments = [dict(self.seg(400_000, 1000), speaker="A")]
        self.assertEqual(clip.window_segments(segments, (300.0, 5400.0))[0]["speaker"], "A")


class CutInvocation(unittest.TestCase):
    """What we hand ffmpeg. The bytes are ffmpeg's problem; the flags are ours."""

    def setUp(self):
        self.calls = []
        self.real_run = subprocess.run
        self.tmp = tempfile.TemporaryDirectory()
        self.dest = str(Path(self.tmp.name) / "clip.mp4")

        def fake_run(cmd, **kwargs):
            self.calls.append(cmd)
            Path(cmd[-1]).write_bytes(b"video")
            return subprocess.CompletedProcess(cmd, 0, "", "")

        clip.subprocess.run = fake_run

    def tearDown(self):
        clip.subprocess.run = self.real_run
        self.tmp.cleanup()

    def test_seek_comes_before_the_input(self):
        # -ss AFTER -i decodes and discards everything up to the start, which
        # on a 90-minute lecture is minutes of CPU instead of seconds.
        clip.cut("src.mp4", self.dest, (300.0, 5400.0), reencode=False)
        cmd = self.calls[0]
        self.assertLess(cmd.index("-ss"), cmd.index("-i"))

    def test_duration_not_end_timestamp(self):
        # With -ss before -i the input timestamps are rebased, so -to would be
        # measured from the wrong origin and the clip would run long.
        clip.cut("src.mp4", self.dest, (300.0, 5400.0), reencode=False)
        cmd = self.calls[0]
        self.assertIn("-t", cmd)
        self.assertNotIn("-to", cmd)
        self.assertEqual(cmd[cmd.index("-t") + 1], "5100.000")

    def test_an_open_end_passes_no_duration(self):
        clip.cut("src.mp4", self.dest, (300.0, None), reencode=False)
        self.assertNotIn("-t", self.calls[0])

    def test_timestamps_are_rebased_to_zero(self):
        # Without this the clip's first frame is still stamped 00:05:00 and the
        # clip-relative timebase this module promises is silently absolute.
        clip.cut("src.mp4", self.dest, (300.0, 5400.0), reencode=False)
        cmd = self.calls[0]
        self.assertEqual(cmd[cmd.index("-avoid_negative_ts") + 1], "make_zero")

    def test_stream_copy_by_default(self):
        clip.cut("src.mp4", self.dest, (300.0, 5400.0), reencode=False)
        self.assertIn("copy", self.calls[0])
        self.assertNotIn("libx264", self.calls[0])

    def test_reencode_is_opt_in(self):
        clip.cut("src.mp4", self.dest, (300.0, 5400.0), reencode=True)
        self.assertIn("libx264", self.calls[0])

    def test_the_write_lands_on_a_part_file_first(self):
        # A half-written clip that looks like a finished artifact would be
        # taken as done by the resume logic and fail two stages later.
        clip.cut("src.mp4", self.dest, (300.0, 5400.0), reencode=False)
        written = self.calls[0][-1]
        self.assertIn(".part", written)
        self.assertNotEqual(written, self.dest)
        self.assertTrue(Path(self.dest).exists())
        self.assertFalse(Path(written).exists())

    def test_the_part_file_keeps_the_destination_extension(self):
        # ffmpeg picks its muxer from the output extension. A temporary named
        # "clip.mp4.part" ends in an extension it does not know, and it
        # refuses to start at all: "Unable to choose an output format". So the
        # partial is "clip.part.mp4". Caught by a real ffmpeg run; the stub
        # that writes whatever path it is handed cannot see it.
        clip.cut("src.mp4", self.dest, (300.0, 5400.0), reencode=False)
        written = self.calls[0][-1]
        self.assertTrue(written.endswith(".mp4"), written)
        self.assertTrue(Path(written).name.startswith("clip.part"), written)


class CutFailure(unittest.TestCase):
    def setUp(self):
        self.real_run = subprocess.run
        self.tmp = tempfile.TemporaryDirectory()
        self.dest = str(Path(self.tmp.name) / "clip.mp4")

    def tearDown(self):
        clip.subprocess.run = self.real_run
        self.tmp.cleanup()

    def test_a_failing_ffmpeg_raises_and_leaves_nothing_behind(self):
        def fake_run(cmd, **kwargs):
            Path(cmd[-1]).write_bytes(b"")
            return subprocess.CompletedProcess(cmd, 1, "", "moov atom not found")
        clip.subprocess.run = fake_run
        with self.assertRaises(clip.ClipError):
            clip.cut("src.mp4", self.dest, (300.0, 5400.0), reencode=False)
        self.assertFalse(Path(self.dest).exists())
        self.assertFalse(Path(self.dest + ".part").exists())

    def test_an_empty_output_is_a_failure_even_when_ffmpeg_exits_zero(self):
        # A window past the end of the video is the case that produces this:
        # ffmpeg is content, and the file is zero bytes.
        def fake_run(cmd, **kwargs):
            Path(cmd[-1]).write_bytes(b"")
            return subprocess.CompletedProcess(cmd, 0, "", "")
        clip.subprocess.run = fake_run
        with self.assertRaises(clip.ClipError) as ctx:
            clip.cut("src.mp4", self.dest, (300.0, 5400.0), reencode=False)
        self.assertIn("past the end", str(ctx.exception))
        self.assertFalse(Path(self.dest).exists())


class CommandLine(unittest.TestCase):
    """The CLI is what pipeline.sh and transcribe.sh actually call."""

    def run_cli(self, args, stdin=None):
        return subprocess.run(
            [sys.executable, str(Path(clip.__file__)), *args],
            input=stdin, capture_output=True, text=True)

    def test_parse_prints_json_the_shell_can_sed(self):
        proc = self.run_cli(["parse", "00:05:00-01:30:00"])
        self.assertEqual(proc.returncode, 0)
        payload = json.loads(proc.stdout)
        self.assertEqual(payload["label"], "00:05:00-01:30:00")
        self.assertEqual(payload["token"], "c000500-013000")
        self.assertEqual(payload["start"], 300.0)
        self.assertEqual(payload["end"], 5400.0)

    def test_a_bad_window_exits_nonzero_with_a_readable_message(self):
        # pipeline.sh parses before it classifies anything, so this is the
        # error the operator sees a second after typing — before a download
        # and before an AssemblyAI charge.
        proc = self.run_cli(["parse", "01:30:00-00:05:00"])
        self.assertEqual(proc.returncode, 1)
        self.assertIn("ends at or before it starts", proc.stderr)

    def test_segments_reads_stdin_and_writes_stdout(self):
        segments = [{"text": "a", "offset_ms": 0, "duration_ms": 1000},
                    {"text": "b", "offset_ms": 400_000, "duration_ms": 1000}]
        proc = self.run_cli(["segments", "00:05:00-01:30:00"],
                            stdin=json.dumps(segments))
        self.assertEqual(proc.returncode, 0)
        kept = json.loads(proc.stdout)
        self.assertEqual([s["text"] for s in kept], ["b"])
        self.assertEqual(kept[0]["offset_ms"], 100_000)


if __name__ == "__main__":
    unittest.main(verbosity=2)
