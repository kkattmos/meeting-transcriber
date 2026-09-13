#!/usr/bin/env python3
"""
Unit tests for the parts of the summarize stage that don't need an API key:
retry classification/backoff, transcript chunking, map-reduce, the document
wrapper, and how the claude-cli backend builds and reads its subprocess call.

    python3 summarize/test_summarize_units.py

The LLM itself is stubbed, so this exercises our logic (what counts as
retryable, how chunks are cut, what the final markdown looks like) rather than
any provider's behaviour.
"""
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import chunking  # noqa: E402
import document  # noqa: E402
import llm_client  # noqa: E402
import retry  # noqa: E402
from chunking import Chunk, Segment  # noqa: E402
from llm_client import BackendUnavailable, ClaudeCliError, FrameMeta  # noqa: E402
from mapreduce import summarize_chunked  # noqa: E402


def _unpack_stdin(raw):
    """(text, image blocks) from what summarize_claude_cli piped to the CLI.

    On the inline path stdin is one stream-json user message; on the Read
    path and with vision off it is the plain prompt. Both come back as the
    prompt text the model reads, so one assertion style serves every test.
    """
    try:
        obj = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return raw, []
    texts, images = [], []
    for block in obj["message"]["content"]:
        if block["type"] == "text":
            texts.append(block["text"])
        else:
            images.append(block)
    return "\n".join(texts), images


class FakeStatusError(Exception):
    """Stands in for an SDK exception carrying an HTTP status."""
    def __init__(self, status, message="boom", headers=None):
        super().__init__(f"{message} (status {status})")
        self.status_code = status
        if headers is not None:
            self.response = type("R", (), {"status_code": status,
                                           "headers": headers})()


class RetryClassificationTest(unittest.TestCase):
    def test_503_is_retryable(self):
        self.assertTrue(retry.is_retryable(FakeStatusError(503)))

    def test_other_transient_statuses_are_retryable(self):
        for status in (408, 429, 500, 502, 504):
            self.assertTrue(retry.is_retryable(FakeStatusError(status)), status)

    def test_client_errors_are_not_retryable(self):
        # A bad key or malformed request fails identically forever; retrying
        # only delays the fallback to a backend that would have worked.
        for status in (400, 401, 403, 404, 422):
            self.assertFalse(retry.is_retryable(FakeStatusError(status)), status)

    def test_network_errors_are_retryable(self):
        self.assertTrue(retry.is_retryable(ConnectionError("reset by peer")))
        self.assertTrue(retry.is_retryable(TimeoutError("timed out")))

    def test_message_only_overload_is_retryable(self):
        """Gemini and Anthropic often raise without a usable status code."""
        self.assertTrue(retry.is_retryable(RuntimeError("503 UNAVAILABLE: overloaded")))
        self.assertTrue(retry.is_retryable(RuntimeError("The model is overloaded")))
        self.assertTrue(retry.is_retryable(RuntimeError("server is busy, try again")))

    def test_plain_value_error_is_not_retryable(self):
        self.assertFalse(retry.is_retryable(ValueError("bad prompt template")))


class RetryBehaviourTest(unittest.TestCase):
    def setUp(self):
        self.slept = []
        os.environ["SUMMARY_MAX_RETRIES"] = "4"
        os.environ["SUMMARY_RETRY_BASE_SECONDS"] = "2"

    def tearDown(self):
        os.environ.pop("SUMMARY_MAX_RETRIES", None)
        os.environ.pop("SUMMARY_RETRY_BASE_SECONDS", None)

    def _sleep(self, seconds):
        self.slept.append(seconds)

    def test_succeeds_after_transient_failures(self):
        calls = {"n": 0}

        def flaky():
            calls["n"] += 1
            if calls["n"] < 3:
                raise FakeStatusError(503)
            return "summary text"

        result = retry.with_retries(flaky, label="test", sleep=self._sleep)
        self.assertEqual(result, "summary text")
        self.assertEqual(calls["n"], 3)
        self.assertEqual(len(self.slept), 2)

    def test_gives_up_after_max_attempts_and_reraises(self):
        calls = {"n": 0}

        def always_busy():
            calls["n"] += 1
            raise FakeStatusError(503)

        with self.assertRaises(FakeStatusError):
            retry.with_retries(always_busy, label="test", sleep=self._sleep)
        # 4 attempts total, so 3 sleeps — the last failure doesn't sleep before
        # handing over to the next backend in the chain.
        self.assertEqual(calls["n"], 4)
        self.assertEqual(len(self.slept), 3)

    def test_non_retryable_fails_immediately(self):
        calls = {"n": 0}

        def bad_key():
            calls["n"] += 1
            raise FakeStatusError(401)

        with self.assertRaises(FakeStatusError):
            retry.with_retries(bad_key, label="test", sleep=self._sleep)
        self.assertEqual(calls["n"], 1)
        self.assertEqual(self.slept, [])

    def test_retry_after_header_wins_over_backoff(self):
        def busy():
            raise FakeStatusError(429, headers={"retry-after": "7"})

        with self.assertRaises(FakeStatusError):
            retry.with_retries(busy, label="test", sleep=self._sleep)
        self.assertTrue(all(s == 7.0 for s in self.slept), self.slept)

    def test_absurd_retry_after_is_ignored(self):
        """An hour-long Retry-After means give up, not sleep through the run."""
        def busy():
            raise FakeStatusError(503, headers={"retry-after": "3600"})

        with self.assertRaises(FakeStatusError):
            retry.with_retries(busy, label="test", sleep=self._sleep)
        self.assertTrue(all(s <= 60 for s in self.slept), self.slept)

    def test_backoff_grows_and_is_capped(self):
        raw = [retry.backoff_seconds(n, base=2, cap=60, jitter=False)
               for n in range(1, 8)]
        self.assertEqual(raw[:5], [2, 4, 8, 16, 32])
        self.assertTrue(all(v <= 60 for v in raw))

    def test_jitter_spreads_parallel_retries(self):
        """Without jitter, concurrent chunks retry in lockstep at a busy server."""
        values = {retry.backoff_seconds(3, base=2, cap=60) for _ in range(50)}
        self.assertGreater(len(values), 10)


class ChunkingTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _frames(self, *timestamps):
        return [FrameMeta(timestamp_s=t, kind="periodic", path=f"/f/{t}.jpg")
                for t in timestamps]

    def test_short_transcript_is_not_chunked(self):
        self.assertEqual(chunking.build_chunks("short text", []), [])

    def test_chunk_disabled_by_zero(self):
        self.assertFalse(chunking.should_chunk("x" * 100000, limit=0))

    def test_srt_parsing(self):
        srt = self.tmp / "a.srt"
        srt.write_text(
            "1\n00:00:00,000 --> 00:00:05,000\nhello there\n\n"
            "2\n00:01:30,500 --> 00:01:35,000\nsecond line\n"
        )
        segments = chunking.parse_srt(srt)
        self.assertEqual(len(segments), 2)
        self.assertAlmostEqual(segments[0].start_s, 0.0)
        self.assertAlmostEqual(segments[1].start_s, 90.5)
        self.assertEqual(segments[1].text, "second line")

    def test_frames_land_in_the_chunk_that_was_on_screen(self):
        segments = [Segment(i * 10.0, i * 10.0 + 10.0, "word " * 30)
                    for i in range(10)]
        frames = self._frames(5.0, 15.0, 55.0, 95.0)
        chunks = chunking.chunk_by_segments(segments, frames, limit=400, overlap=0)
        self.assertGreater(len(chunks), 1)
        # Every frame is assigned exactly once — none dropped, none duplicated.
        assigned = [f.timestamp_s for c in chunks for f in c.frames]
        self.assertEqual(sorted(assigned), [5.0, 15.0, 55.0, 95.0])
        for chunk in chunks:
            for frame in chunk.frames:
                self.assertGreaterEqual(frame.timestamp_s, chunk.start_s)

    def test_trailing_frame_after_last_word_is_kept(self):
        segments = [Segment(0.0, 10.0, "a" * 100), Segment(10.0, 20.0, "b" * 100)]
        frames = self._frames(500.0)  # a slide left up long after the talking
        chunks = chunking.chunk_by_segments(segments, frames, limit=50, overlap=0)
        assigned = [f.timestamp_s for c in chunks for f in c.frames]
        self.assertIn(500.0, assigned)

    def test_overlap_repeats_context_between_chunks(self):
        segments = [Segment(i * 5.0, i * 5.0 + 5.0, f"sentence{i} " * 10)
                    for i in range(12)]
        no_overlap = chunking.chunk_by_segments(segments, [], limit=300, overlap=0)
        with_overlap = chunking.chunk_by_segments(segments, [], limit=300, overlap=100)
        total_no = sum(len(c.text) for c in no_overlap)
        total_with = sum(len(c.text) for c in with_overlap)
        self.assertGreater(total_with, total_no)

    def test_text_fallback_when_no_srt(self):
        transcript = "\n".join(f"line {i} " + "x" * 50 for i in range(100))
        frames = self._frames(*[float(i) for i in range(20)])
        chunks = chunking.chunk_by_text(transcript, frames, limit=1000, overlap=0)
        self.assertGreater(len(chunks), 1)
        assigned = sum(len(c.frames) for c in chunks)
        self.assertEqual(assigned, 20)

    def test_build_chunks_prefers_the_srt_sibling(self):
        txt = self.tmp / "run.txt"
        srt = self.tmp / "run.srt"
        long_line = "word " * 200
        txt.write_text("\n".join([long_line] * 40))
        srt.write_text("\n\n".join(
            f"{i}\n00:00:{i:02d},000 --> 00:00:{i + 1:02d},000\n{long_line}"
            for i in range(40)
        ))
        chunks = chunking.build_chunks(txt.read_text(), [], str(txt))
        self.assertGreater(len(chunks), 1)
        # Timestamps only exist on the srt path, so this proves it was used.
        self.assertIsNotNone(chunks[0].start_s)


class MapReduceTest(unittest.TestCase):
    def _chunks(self, n):
        return [Chunk(index=i, text=f"body {i}", start_s=i * 60.0,
                      end_s=(i + 1) * 60.0, frames=[]) for i in range(n)]

    def test_chunks_are_summarized_then_merged(self):
        calls = []

        def fake_summarize(frames, transcript, template, role=None):
            calls.append(transcript)
            if "Partial summaries" in template or "merging" in template.lower():
                return "MERGED DOCUMENT"
            return f"summary of: {transcript}"

        out = summarize_chunked(self._chunks(3), "PROMPT {transcript}",
                                fake_summarize, log=lambda *a: None)
        self.assertEqual(out, "MERGED DOCUMENT")
        self.assertEqual(len(calls), 4)  # 3 chunks + 1 merge

    def test_partial_failure_still_produces_a_document(self):
        """Two good chunks cost real API calls; one bad one mustn't waste them."""
        def fake_summarize(frames, transcript, template, role=None):
            if transcript == "body 1":
                raise RuntimeError("503 overloaded")
            if "merging" in template.lower() or "Partial summaries" in template:
                return "MERGED"
            return f"ok {transcript}"

        out = summarize_chunked(self._chunks(3), "PROMPT {transcript}",
                                fake_summarize, log=lambda *a: None)
        self.assertIn("MERGED", out)
        self.assertIn("Incomplete", out)
        self.assertIn("part(s) 2 of 3", out)

    def test_all_chunks_failing_raises(self):
        def fake_summarize(frames, transcript, template, role=None):
            raise RuntimeError("everything is down")

        with self.assertRaises(RuntimeError):
            summarize_chunked(self._chunks(2), "P {transcript}",
                              fake_summarize, log=lambda *a: None)

    def test_chunk_order_is_preserved_despite_parallelism(self):
        import time

        def fake_summarize(frames, transcript, template, role=None):
            if "Partial summaries" in template:
                return transcript  # hand the combined text back for inspection
            # Make the first chunk the slowest, so completion order != index.
            time.sleep(0.05 if transcript == "body 0" else 0.0)
            return f"S{transcript[-1]}"

        combined = summarize_chunked(self._chunks(3), "P {transcript}",
                                     fake_summarize, log=lambda *a: None)
        self.assertLess(combined.index("S0"), combined.index("S1"))
        self.assertLess(combined.index("S1"), combined.index("S2"))


class DocumentTest(unittest.TestCase):
    def test_wrapper_applies_to_lecture_and_tutorial_only(self):
        self.assertTrue(document.wants_wrapper("lecture-gemini"))
        self.assertTrue(document.wants_wrapper("tutorial-claude.md"))
        self.assertFalse(document.wants_wrapper("meeting-gemini"))
        self.assertFalse(document.wants_wrapper(None))

    def test_format_override(self):
        self.assertTrue(document.wants_wrapper("meeting-gemini", "always"))
        self.assertFalse(document.wants_wrapper("lecture-gemini", "never"))

    def test_document_shape_matches_the_course_template(self):
        out = document.build_document(
            "## 1. Background\nSome content.",
            source="https://www.youtube.com/watch?v=abc123",
            source_kind="youtube",
            title="Chapter01 SRS n UI 1",
            transcript="line one\nline two",
            backend="gemini", model="gemini-2.5-flash",
            prompt_name="lecture-gemini.md", run_id="yt_abc123_20260809_120000",
            generated="2026-08-09",
        )
        self.assertTrue(out.startswith("<!-- meeting-transcriber"))
        self.assertIn("prompt: lecture-gemini.md", out)
        self.assertIn("model: gemini/gemini-2.5-flash", out)
        self.assertIn("Chapter N — <topic> (<date>)", out)
        self.assertIn("# Chapter01 SRS n UI 1", out)
        self.assertIn("Youtube Link: `https://www.youtube.com/watch?v=abc123`", out)
        self.assertIn("<details>", out)
        self.assertIn("    <summary> View Transcript </summary>", out)
        self.assertIn("    line one", out)   # 4-space indent, as in chapter1.md
        self.assertIn("</details>", out)
        self.assertIn("## 1. Background", out)
        self.assertTrue(out.rstrip().endswith("<br><br>"))

    def test_provenance_cannot_break_out_of_the_comment(self):
        """A '-->' in the source must not end the comment early.

        Content inside the comment is inert; what matters is that the comment
        stays closed, so nothing leaks into the rendered page.
        """
        out = document.build_document(
            "body", source="https://x/--><script>alert(1)</script>",
            source_kind="youtube", title="t", transcript="x")
        header, _, rest = out.partition("-->")
        # The first "-->" in the document is the comment terminator, not one
        # smuggled in through the source value.
        self.assertIn("meeting-transcriber", header)
        self.assertNotIn("-->", header)
        self.assertIn("--&gt;", header)
        # Everything after the comment is the document proper.
        self.assertTrue(rest.lstrip().startswith(document.CHAPTER_PLACEHOLDER))

    def test_a_clip_is_declared_in_the_document_and_the_provenance(self):
        # Every timestamp in a clipped summary — the SRT it quotes, the frame
        # citations, the model's own references — is measured from the start of
        # the clip, because the media was cut before any of them existed. A
        # reader who is not told that will scrub to the wrong place in the
        # source and conclude the summary is wrong, so it is stated visibly and
        # not only in the greppable comment.
        out = document.build_document(
            "body", source="https://www.youtube.com/watch?v=abc123",
            source_kind="youtube", title="A", transcript="x",
            clip="00:05:00-01:30:00")
        header, _, body = out.partition("-->")
        self.assertIn("clip: 00:05:00-01:30:00", header)
        self.assertIn("Clip: `00:05:00-01:30:00`", body)
        self.assertIn("relative to the start of the clip", body)

    def test_an_unclipped_document_says_nothing_about_clips(self):
        out = document.build_document(
            "body", source="https://www.youtube.com/watch?v=abc123",
            source_kind="youtube", title="A", transcript="x")
        self.assertNotIn("Clip:", out)
        self.assertNotIn("clip:", out)

    def test_local_file_source_label(self):
        out = document.build_document(
            "body", source="/opt/meeting-bot/recordings/a.mp4",
            source_kind="local_file", title="A", transcript="x")
        self.assertIn("Source File: `/opt/meeting-bot/recordings/a.mp4`", out)
        self.assertNotIn("Youtube Link", out)

    def test_a_combined_document_links_every_video_and_says_so(self):
        # --combine: one document, one title, one link line per video tagged
        # with the number the model's citations use, and the one thing the
        # reader has to be told — that every clock is its own video's.
        out = document.build_document(
            "See (Frame 7 @ video 2 30.0s).",
            source="https://youtu.be/v1", source_kind="youtube",
            transcript="t",
            videos=[
                {"source": "https://youtu.be/v1", "kind": "youtube",
                 "title": "Week 1"},
                {"source": "/rec/w2.mp4", "kind": "local_file",
                 "clip": "00:05:00-end"},
                {"source": "https://k/p/1/embedPlaykitJs/uiconf_id/0?entry_id=x",
                 "kind": "kaltura", "title": "Week 3"},
            ])
        self.assertEqual(out.count("\n# "), 1)
        self.assertIn("# Week 1", out)
        self.assertNotIn("# Week 3", out)
        self.assertIn("Youtube Link (Video 1): `https://youtu.be/v1`", out)
        self.assertIn("Source File (Video 2): `/rec/w2.mp4`", out)
        self.assertIn("Clip (Video 2): `00:05:00-end`", out)
        self.assertIn("Video Link (Video 3):", out)
        self.assertIn("Summarized from 3 videos as one", out)
        self.assertIn("relative to the start of the video", out)
        # Provenance carries the set too, one line per video.
        self.assertIn("source_type: combined", out)
        self.assertIn("videos: 3", out)
        self.assertIn("video_2: /rec/w2.mp4", out)
        self.assertIn("video_2_clip: 00:05:00-end", out)
        # Exactly one transcript block — the text was joined before it got
        # here — and the body follows it.
        self.assertEqual(out.count("<details>"), 1)
        self.assertIn("See (Frame 7 @ video 2 30.0s).", out)

    def test_an_explicit_title_beats_the_first_videos(self):
        out = document.build_document(
            "b", source="s", source_kind="youtube", title="Chapter 3",
            transcript="t",
            videos=[{"source": "s", "kind": "youtube", "title": "Week 1"}])
        self.assertIn("# Chapter 3", out)
        self.assertNotIn("# Week 1", out)

    def test_a_single_video_document_is_untouched_by_the_videos_path(self):
        out = document.build_document(
            "b", source="https://youtu.be/v", source_kind="youtube",
            title="V", transcript="t")
        self.assertIn("Youtube Link: `https://youtu.be/v`", out)
        self.assertNotIn("(Video 1)", out)
        self.assertNotIn("Summarized from", out)
        self.assertNotIn("combined", out)


class CombinedPartsTest(unittest.TestCase):
    """Several videos summarized as one (pipeline.sh --combine).

    Two invariants matter. Frame numbers are unique across the whole set —
    the model is shown one number per picture and the PDF resolves the same
    one — and every timestamp stays relative to its own video, with the
    video named wherever a timestamp appears, because "410.0s" on its own
    no longer says which lecture.
    """

    def frames(self, part, *stamps):
        return [FrameMeta(timestamp_s=t, kind="periodic",
                          path=f"/f/{part}/{t}.jpg", part=part)
                for t in stamps]

    def test_numbers_run_across_videos_in_video_order(self):
        # Video 2's frames come after video 1's even though their timestamps
        # are smaller: the clock restarts per video.
        frames = llm_client.assign_numbers(
            self.frames(2, 10.0, 20.0) + self.frames(1, 30.0, 40.0))
        self.assertEqual([(f.part, f.timestamp_s, f.number) for f in frames],
                         [(1, 30.0, 1), (1, 40.0, 2), (2, 10.0, 3), (2, 20.0, 4)])

    def test_the_manifest_line_names_the_video(self):
        frames = llm_client.assign_numbers(
            self.frames(1, 30.0) + self.frames(2, 30.0))
        _, text = llm_client._render(frames, "t", "{frame_manifest}")
        self.assertIn("[frame 1 @ video 1 30.0s (periodic)]", text)
        self.assertIn("[frame 2 @ video 2 30.0s (periodic)]", text)
        # And the render keeps video order, not timestamp order.
        self.assertLess(text.index("frame 1 @"), text.index("frame 2 @"))

    def test_a_single_recording_frame_label_is_unchanged(self):
        frame = FrameMeta(timestamp_s=5.0, kind="scene_change", path="/f.jpg")
        self.assertEqual(frame.label(3), "[frame 3 @ 5.0s (scene_change)]")

    def test_the_part_transcript_fences_each_video(self):
        parts = [chunking.Part(label="video 1 of 2: A", text="alpha"),
                 chunking.Part(label="video 2 of 2: B", text="beta")]
        text = chunking.part_transcript(parts)
        self.assertEqual(text, "=== video 1 of 2: A ===\n\nalpha\n\n"
                               "=== video 2 of 2: B ===\n\nbeta")

    def test_short_videos_go_in_one_call(self):
        parts = [chunking.Part(label="video 1 of 2: A", text="a" * 100),
                 chunking.Part(label="video 2 of 2: B", text="b" * 100)]
        self.assertEqual(chunking.build_part_chunks(parts, limit=1000), [])

    def test_long_sets_are_chunked_per_video_and_labelled(self):
        with tempfile.TemporaryDirectory() as tmp:
            srt = Path(tmp) / "a.srt"
            srt.write_text(
                "1\n00:00:00,000 --> 00:00:10,000\n" + "x" * 60 + "\n\n"
                "2\n00:00:10,000 --> 00:00:20,000\n" + "y" * 60 + "\n\n"
                "3\n00:00:20,000 --> 00:00:30,000\n" + "z" * 60 + "\n")
            f1 = llm_client.assign_numbers(
                self.frames(1, 5.0, 15.0, 25.0) + self.frames(2, 5.0))
            parts = [
                chunking.Part(label="video 1 of 2: A",
                              text="x" * 60 + "\n" + "y" * 60 + "\n" + "z" * 60,
                              frames=[f for f in f1 if f.part == 1],
                              srt_path=str(srt)),
                chunking.Part(label="video 2 of 2: B", text="short",
                              frames=[f for f in f1 if f.part == 2]),
            ]
            with mock.patch.dict(os.environ, {"SUMMARY_CHUNK_OVERLAP": "0"}):
                chunks = chunking.build_part_chunks(parts, limit=100)
        # Video 1 split on its own segments (one per 60-char cue under a
        # 100-char limit), video 2 is one chunk of its own even though it
        # would have fitted into video 1's last.
        self.assertEqual(len(chunks), 4)
        self.assertEqual([c.index for c in chunks], [0, 1, 2, 3])
        self.assertEqual([c.label for c in chunks],
                         ["video 1 of 2: A"] * 3 + ["video 2 of 2: B"])
        # Each chunk carries only its own video's frames, under their global
        # numbers.
        self.assertEqual([f.number for c in chunks[:3] for f in c.frames],
                         [1, 2, 3])
        self.assertEqual([f.number for f in chunks[3].frames], [4])
        # The header names the video, and its window is that video's clock.
        self.assertIn("video 1 of 2: A", chunks[0].header(4))
        self.assertIn("00:00:00–00:00:10", chunks[0].header(4))
        self.assertEqual(chunks[3].header(4), "part 4 of 4 (video 2 of 2: B)")

    def test_a_video_with_no_srt_still_gets_one_chunk(self):
        parts = [chunking.Part(label="video 1 of 1: A", text="a\n" * 200)]
        chunks = chunking.build_part_chunks(parts, limit=100)
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(c.label == "video 1 of 1: A" for c in chunks))


# ---------------------------------------------------------------------------
# The claude-cli backend
#
# The summarizer spends a Claude subscription by running `claude -p`, so the
# things that can break are the command line it builds, the environment it
# hands the child, and how it reads the CLI's JSON envelope back. None of that
# needs a network or a login, so all of it is tested here; lib/test_media_e2e.sh
# covers the same ground against a stub binary in a real run.
# ---------------------------------------------------------------------------

def _envelope(result, is_error=False, denials=()):
    """The shape `claude -p --output-format json` prints."""
    return json.dumps({
        "type": "result",
        "subtype": "error" if is_error else "success",
        "is_error": is_error,
        "result": result,
        "permission_denials": list(denials),
    })


class FakeCompleted:
    def __init__(self, stdout="", stderr="", returncode=0):
        self.stdout, self.stderr, self.returncode = stdout, stderr, returncode


class ClaudeCliEnvironmentTest(unittest.TestCase):
    """The subprocess environment decides which account pays."""

    def test_api_key_vars_are_stripped(self):
        # A set ANTHROPIC_API_KEY silently moves the spend from the
        # subscription to a metered account, and the summary looks identical —
        # so this is the only place the mistake is visible.
        with mock.patch.dict(os.environ, {
                "ANTHROPIC_API_KEY": "sk-ant-live",
                "ANTHROPIC_AUTH_TOKEN": "tok",
                "ANTHROPIC_BASE_URL": "https://proxy.example",
                "PATH": os.environ.get("PATH", "")}):
            env = llm_client._claude_cli_env()
        for var in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN",
                    "ANTHROPIC_BASE_URL"):
            self.assertNotIn(var, env)

    def test_unrelated_vars_survive(self):
        with mock.patch.dict(os.environ, {"HOME": "/root", "LANG": "th_TH.UTF-8"}):
            env = llm_client._claude_cli_env()
        self.assertEqual(env["LANG"], "th_TH.UTF-8")

    def test_cwd_is_not_the_repo(self):
        # The CLI auto-discovers CLAUDE.md from its cwd, and this repo's is
        # 38KB of architecture notes with nothing to do with the lecture.
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(os.environ, {"MEETING_BOT_ROOT": tmp}):
                cwd = Path(llm_client._claude_cli_cwd())
        self.assertTrue(str(cwd).startswith(tmp))
        self.assertNotEqual(cwd.resolve(),
                            Path(__file__).resolve().parent.parent)


class ClaudeCliCommandLineTest(unittest.TestCase):
    """What ends up in argv, and what ends up in the prompt."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        frame_dir = Path(self.tmp.name) / "frames"
        frame_dir.mkdir()
        self.frames = []
        for i, ts in enumerate((30.0, 5.0), start=1):   # deliberately unsorted
            fp = frame_dir / f"frame_{i:04d}.jpg"
            fp.write_bytes(b"\xff\xd8\xff\xe0stub")
            self.frames.append(FrameMeta(timestamp_s=ts, kind="periodic",
                                         path=str(fp)))
        self.frame_dir = frame_dir
        self.calls = []

    def _run(self, env_overrides=None, frames=None, result=None):
        """Drive summarize_claude_cli with subprocess.run intercepted."""
        def fake_run(argv, **kwargs):
            self.calls.append((argv, kwargs))
            return FakeCompleted(stdout=result or _envelope("SUMMARY BODY"))

        env = {"MEETING_BOT_ROOT": self.tmp.name,
               "CLAUDE_CLI_BIN": sys.executable}
        env.update(env_overrides or {})
        with mock.patch.dict(os.environ, env), \
             mock.patch.object(llm_client.subprocess, "run", fake_run):
            out = llm_client.summarize_claude_cli(
                self.frames if frames is None else frames,
                "Dijkstra runs in O(E log V).",
                "Notes:\n{transcript}\nFrames:\n{frame_manifest}")
        return out

    def _argv(self):
        return self.calls[0][0]

    def _prompt(self):
        return _unpack_stdin(self.calls[0][1]["input"])[0]

    def _images(self):
        return _unpack_stdin(self.calls[0][1]["input"])[1]

    def test_effort_is_passed_through(self):
        self._run({"SUMMARY_EFFORT": "xhigh"})
        argv = self._argv()
        self.assertIn("--effort", argv)
        self.assertEqual(argv[argv.index("--effort") + 1], "xhigh")

    def test_invalid_effort_falls_back_to_high(self):
        # A typo reaching the CLI comes back as an opaque usage error mid-run.
        self._run({"SUMMARY_EFFORT": "maximum"})
        argv = self._argv()
        self.assertEqual(argv[argv.index("--effort") + 1], "high")

    def test_model_defaults_to_the_opus_alias(self):
        # An alias, not a pinned id: a model rename must not 404 a box nobody
        # has touched in a year.
        self._run()
        argv = self._argv()
        self.assertEqual(argv[argv.index("--model") + 1], "opus")

    def test_anthropic_model_is_accepted_as_an_alias(self):
        self._run({"ANTHROPIC_MODEL": "claude-opus-5"})
        argv = self._argv()
        self.assertEqual(argv[argv.index("--model") + 1], "claude-opus-5")

    def test_print_mode_and_streaming_json_output(self):
        # stream-json, not json: it is the only format that carries the
        # rate_limit_event — the subscription's own meter and, on a hit
        # window, the reset time. Print mode needs --verbose to allow it.
        self._run()
        argv = self._argv()
        self.assertIn("-p", argv)
        self.assertEqual(argv[argv.index("--output-format") + 1], "stream-json")
        self.assertIn("--verbose", argv)

    def test_frame_cap_thins_what_the_model_is_offered(self):
        # Ten frames, cap 4: the manifest in the prompt names four, and the
        # ones it names keep their global numbers.
        frames = [FrameMeta(timestamp_s=float(i * 30), kind="periodic",
                            path=self.frames[0].path, number=i + 1)
                  for i in range(10)]
        self._run({"CLAUDE_CLI_MAX_FRAMES": "4", "CLAUDE_CLI_FRAME_VISION": "0"},
                  frames=frames)
        prompt = self._prompt()
        cited = [line for line in prompt.splitlines() if line.startswith("[frame ")]
        self.assertEqual(len(cited), 4)
        self.assertTrue(cited[0].startswith("[frame 1 @ 0.0s"))
        self.assertTrue(cited[-1].startswith("[frame 10 @ 270.0s"))

    def test_no_frame_cap_by_default(self):
        frames = [FrameMeta(timestamp_s=float(i * 30), kind="periodic",
                            path=self.frames[0].path, number=i + 1)
                  for i in range(10)]
        self._run({"CLAUDE_CLI_FRAME_VISION": "0"}, frames=frames)
        prompt = self._prompt()
        self.assertEqual(sum(1 for l in prompt.splitlines() if l.startswith("[frame ")), 10)

    def test_context_is_isolated(self):
        self._run()
        argv = self._argv()
        self.assertIn("--safe-mode", argv)
        self.assertIn("--no-session-persistence", argv)

    def test_no_budget_tokens_anywhere(self):
        # The parameter current models reject has no CLI spelling at all now.
        self._run()
        self.assertNotIn("budget_tokens", " ".join(self._argv()))
        self.assertNotIn("budget_tokens", self._prompt())

    def test_vision_sends_frames_inline_with_no_tools(self):
        # The default: the frames ride along as image blocks in a stream-json
        # user message. One turn, no Read tool, no --add-dir — and so no
        # per-frame turn that resends the whole context as cache reads.
        self._run()
        argv = self._argv()
        self.assertEqual(argv[argv.index("--input-format") + 1], "stream-json")
        self.assertEqual(argv[argv.index("--tools") + 1], "")
        self.assertNotIn("--allowedTools", argv)
        self.assertNotIn("--add-dir", argv)
        images = self._images()
        self.assertEqual(len(images), 2)
        for block in images:
            self.assertEqual(block["type"], "image")
            self.assertEqual(block["source"]["type"], "base64")
            self.assertEqual(block["source"]["media_type"], "image/jpeg")

    def test_inline_frames_are_labelled_with_their_global_numbers(self):
        # The label beside each picture is the number the model cites, and
        # the number the PDF resolves — so it is the frame's own, never the
        # position in this call's list.
        frames = [FrameMeta(timestamp_s=5.0, kind="periodic", number=7,
                            path=self.frames[0].path),
                  FrameMeta(timestamp_s=30.0, kind="scene_change", number=9,
                            path=self.frames[1].path)]
        self._run(frames=frames)
        raw = json.loads(self.calls[0][1]["input"])
        content = raw["message"]["content"]
        labels = [b["text"] for b in content[1:] if b["type"] == "text"]
        self.assertEqual(labels, ["[frame 7 @ 5.0s (periodic)]",
                                  "[frame 9 @ 30.0s (scene_change)]"])
        # label, image, label, image — each picture right after its label
        kinds = [b["type"] for b in content[1:]]
        self.assertEqual(kinds, ["text", "image", "text", "image"])

    def test_inline_prompt_carries_no_filesystem_paths(self):
        self._run()
        prompt = self._prompt()
        self.assertNotIn(str(self.frame_dir), prompt)
        self.assertNotIn("Read tool", prompt)
        self.assertIn("attached after this text", prompt)

    def test_read_path_grants_read_scoped_to_the_frame_dir(self):
        # CLAUDE_CLI_FRAME_INLINE=0: the older delivery, kept for a CLI too
        # old to take stream-json input.
        self._run({"CLAUDE_CLI_FRAME_INLINE": "0"})
        argv = self._argv()
        self.assertNotIn("--input-format", argv)
        self.assertEqual(argv[argv.index("--tools") + 1], "Read")
        self.assertEqual(argv[argv.index("--allowedTools") + 1], "Read")
        self.assertEqual(argv[argv.index("--add-dir") + 1], str(self.frame_dir))
        self.assertEqual(self._images(), [])

    def test_read_path_puts_absolute_frame_paths_in_the_prompt(self):
        self._run({"CLAUDE_CLI_FRAME_INLINE": "0"})
        prompt = self._prompt()
        for frame in self.frames:
            self.assertIn(str(Path(frame.path).resolve()), prompt)
        self.assertIn("Read tool", prompt)

    def test_merge_role_may_use_a_cheaper_model_and_effort(self):
        env = {"CLAUDE_CLI_MODEL": "opus", "SUMMARY_EFFORT": "medium",
               "CLAUDE_CLI_MERGE_MODEL": "sonnet", "SUMMARY_MERGE_EFFORT": "low"}
        with mock.patch.dict(os.environ, env):
            self._run(frames=[])
        argv = self._argv()
        self.assertEqual(argv[argv.index("--model") + 1], "opus")
        self.assertEqual(argv[argv.index("--effort") + 1], "medium")
        self.calls.clear()

        def fake_run(argv, **kwargs):
            self.calls.append((argv, kwargs))
            return FakeCompleted(stdout=_envelope("MERGED"))
        env.update({"MEETING_BOT_ROOT": self.tmp.name,
                    "CLAUDE_CLI_BIN": sys.executable})
        with mock.patch.dict(os.environ, env), \
             mock.patch.object(llm_client.subprocess, "run", fake_run):
            llm_client.summarize_claude_cli([], "partials", "{transcript}{frame_manifest}",
                                            role="merge")
        argv = self._argv()
        self.assertEqual(argv[argv.index("--model") + 1], "sonnet")
        self.assertEqual(argv[argv.index("--effort") + 1], "low")

    def test_merge_role_defaults_to_the_chunk_model(self):
        def fake_run(argv, **kwargs):
            self.calls.append((argv, kwargs))
            return FakeCompleted(stdout=_envelope("MERGED"))
        env = {"MEETING_BOT_ROOT": self.tmp.name, "CLAUDE_CLI_BIN": sys.executable,
               "CLAUDE_CLI_MODEL": "opus", "SUMMARY_EFFORT": "medium"}
        with mock.patch.dict(os.environ, env, clear=False), \
             mock.patch.object(llm_client.subprocess, "run", fake_run):
            os.environ.pop("CLAUDE_CLI_MERGE_MODEL", None)
            os.environ.pop("SUMMARY_MERGE_EFFORT", None)
            llm_client.summarize_claude_cli([], "partials", "{transcript}{frame_manifest}",
                                            role="merge")
        argv = self._argv()
        self.assertEqual(argv[argv.index("--model") + 1], "opus")
        self.assertEqual(argv[argv.index("--effort") + 1], "medium")

    def test_frames_are_listed_in_timestamp_order(self):
        # The manifest numbering is what the model cites, and pdf.py matches
        # those citations back to files — an out-of-order manifest mislabels
        # every picture in the PDF.
        prompt = self._prompt() if self.calls else None
        self._run()
        prompt = self._prompt()
        self.assertLess(prompt.index("@ 5.0s"), prompt.index("@ 30.0s"))

    def test_vision_off_offers_no_tools_and_no_paths(self):
        self._run({"CLAUDE_CLI_FRAME_VISION": "0"})
        argv = self._argv()
        self.assertEqual(argv[argv.index("--tools") + 1], "")
        self.assertNotIn("--add-dir", argv)
        self.assertNotIn(str(self.frame_dir), self._prompt())

    def test_no_frames_means_no_tools(self):
        self._run(frames=[])
        argv = self._argv()
        self.assertEqual(argv[argv.index("--tools") + 1], "")

    def test_transcript_reaches_the_prompt_on_stdin(self):
        # Not argv: an 80KB transcript would blow past ARG_MAX.
        self._run()
        self.assertIn("Dijkstra runs in O(E log V).", self._prompt())
        self.assertNotIn("Dijkstra", " ".join(self._argv()))

    def test_provenance_records_the_backend_that_answered(self):
        self._run({"CLAUDE_CLI_MODEL": "sonnet"})
        self.assertEqual(llm_client.LAST_BACKEND, "claude-cli")
        self.assertEqual(llm_client.LAST_MODEL, "sonnet")

    def test_returns_the_result_text(self):
        self.assertEqual(self._run(), "SUMMARY BODY")


class ClaudeCliResponseTest(unittest.TestCase):
    """Reading the envelope back — the CLI exits 0 even when it failed."""

    def _run(self, stdout="", stderr="", returncode=0):
        def fake_run(argv, **kwargs):
            return FakeCompleted(stdout, stderr, returncode)
        with mock.patch.object(llm_client.subprocess, "run", fake_run):
            return llm_client._run_claude_cli(["claude"], "p", {}, ".", 60)

    def test_not_logged_in_is_unavailable_not_a_failure(self):
        # Exit code 0, is_error true. If this were classified as a transient
        # failure the chain would burn the whole retry schedule on something
        # no retry can fix, instead of falling through to Gemini.
        with self.assertRaises(BackendUnavailable) as ctx:
            self._run(stdout=_envelope("Not logged in · Please run /login",
                                       is_error=True))
        self.assertIn("not logged in", str(ctx.exception).lower())

    def test_not_logged_in_is_never_retried(self):
        exc = BackendUnavailable("claude CLI is not logged in")
        self.assertFalse(retry.is_retryable(exc))

    def test_overloaded_is_retryable(self):
        with self.assertRaises(ClaudeCliError) as ctx:
            self._run(stdout=_envelope("API Error: 503 upstream is overloaded",
                                       is_error=True))
        self.assertTrue(retry.is_retryable(ctx.exception))

    def test_empty_result_is_an_error(self):
        with self.assertRaises(ClaudeCliError):
            self._run(stdout=_envelope("   "))

    def test_non_json_output_is_an_error(self):
        with self.assertRaises(ClaudeCliError):
            self._run(stdout="Usage: claude [options]")

    def test_non_json_login_message_is_still_unavailable(self):
        # An older CLI prints this as bare text before it reaches --output-format.
        with self.assertRaises(BackendUnavailable):
            self._run(stdout="Not logged in · Please run /login")

    def test_no_output_at_all_is_an_error(self):
        with self.assertRaises(ClaudeCliError):
            self._run(stdout="", stderr="segfault", returncode=139)

    def test_permission_denial_warns_but_returns_the_summary(self):
        # The summary exists; it was just written without the frames. Worth a
        # warning, not worth discarding.
        with mock.patch("sys.stderr", new_callable=io.StringIO) as err:
            out = self._run(stdout=_envelope(
                "BODY", denials=[{"tool_name": "Read"}]))
        self.assertEqual(out, "BODY")
        self.assertIn("denied", err.getvalue())

    def test_a_timeout_is_retryable(self):
        def fake_run(argv, **kwargs):
            raise subprocess.TimeoutExpired(cmd="claude", timeout=60)
        with mock.patch.object(llm_client.subprocess, "run", fake_run):
            with self.assertRaises(ClaudeCliError) as ctx:
                llm_client._run_claude_cli(["claude"], "p", {}, ".", 60)
        self.assertTrue(retry.is_retryable(ctx.exception))

    def test_a_missing_binary_is_unavailable(self):
        def fake_run(argv, **kwargs):
            raise OSError(2, "No such file or directory")
        with mock.patch.object(llm_client.subprocess, "run", fake_run):
            with self.assertRaises(BackendUnavailable):
                llm_client._run_claude_cli(["claude"], "p", {}, ".", 60)


class ClaudeCliUsageWindowTest(unittest.TestCase):
    """An exhausted subscription window: detected, waited out, never Gemini.

    SUMMARY_MAX_TOKENS was the operator's first guess at a lever here, and it
    does nothing on this backend; what exists instead is the meter the CLI
    reports and a wait for the reset. These tests hold that path.
    """

    def _stream(self, status, resets_at, result="BODY", is_error=False,
                api_error_status=None, utilization=0.5):
        event = {"type": "rate_limit_event", "rate_limit_info": {
            "status": status, "rateLimitType": "five_hour",
            "unifiedWindows": {"five_hour": {"utilization": utilization,
                                             "resetsAt": resets_at}}}}
        if status == "rejected":
            event["rate_limit_info"]["resetsAt"] = resets_at
        envelope = json.loads(_envelope(result, is_error=is_error))
        envelope["api_error_status"] = api_error_status
        envelope["usage"] = {"input_tokens": 1000, "output_tokens": 50,
                             "cache_read_input_tokens": 300,
                             "output_tokens_details": {"thinking_tokens": 7}}
        envelope["total_cost_usd"] = 0.5
        return "\n".join(json.dumps(o) for o in (
            {"type": "system", "subtype": "init"}, event, envelope))

    def setUp(self):
        llm_client.USAGE.reset()
        self.slept = []
        self._orig_sleep = llm_client._sleep
        llm_client._sleep = lambda s: self.slept.append(s)
        self._orig_hook = llm_client.WAIT_HOOK
        llm_client.WAIT_HOOK = None

    def tearDown(self):
        llm_client._sleep = self._orig_sleep
        llm_client.WAIT_HOOK = self._orig_hook
        llm_client.USAGE.reset()

    def _run_once(self, stdout):
        def fake_run(argv, **kwargs):
            return FakeCompleted(stdout)
        with mock.patch.object(llm_client.subprocess, "run", fake_run):
            return llm_client._run_claude_cli(["claude"], "p", {}, ".", 60)

    def test_stream_output_parses_to_the_result_line(self):
        self.assertEqual(self._run_once(self._stream("allowed", 1_800_000_000)),
                         "BODY")

    def test_plain_envelope_still_parses(self):
        # An older stub, or --output-format json: no event, one object.
        self.assertEqual(self._run_once(_envelope("BODY")), "BODY")

    def test_usage_is_recorded_with_the_meter(self):
        self._run_once(self._stream("allowed", 1_800_000_000, utilization=0.42))
        s = llm_client.USAGE.summary()
        self.assertEqual(s["calls"], 1)
        self.assertEqual(s["input_tokens"], 1000)
        self.assertEqual(s["cache_read_input_tokens"], 300)
        self.assertEqual(s["thinking_tokens"], 7)
        self.assertEqual(s["cost_usd"], 0.5)
        self.assertEqual(s["windows"]["five_hour"]["utilization_after"], 0.42)
        self.assertEqual(s["windows"]["five_hour"]["resets_at"], 1_800_000_000)

    def test_meter_delta_spans_the_stage(self):
        self._run_once(self._stream("allowed", 1_800_000_000, utilization=0.10))
        self._run_once(self._stream("allowed", 1_800_000_000, utilization=0.35))
        five = llm_client.USAGE.summary()["windows"]["five_hour"]
        self.assertEqual(five["utilization_before"], 0.10)
        self.assertEqual(five["utilization_after"], 0.35)
        self.assertAlmostEqual(five["utilization_delta"], 0.25)

    def test_rejected_event_is_rate_limited_with_the_reset_time(self):
        with self.assertRaises(llm_client.ClaudeCliRateLimited) as ctx:
            self._run_once(self._stream("rejected", 1_800_000_000,
                                        result="API Error: 429", is_error=True,
                                        api_error_status=429))
        self.assertEqual(ctx.exception.resets_at, 1_800_000_000)
        self.assertEqual(ctx.exception.window, "five_hour")
        # The spent call is still on the ledger — that is the number that
        # explains the pause.
        self.assertEqual(llm_client.USAGE.summary()["calls"], 1)

    def test_429_without_an_event_is_rate_limited(self):
        env = json.loads(_envelope("API Error: 429 rate limit", is_error=True))
        env["api_error_status"] = 429
        with self.assertRaises(llm_client.ClaudeCliRateLimited) as ctx:
            self._run_once(json.dumps(env))
        self.assertIsNone(ctx.exception.resets_at)

    def test_legacy_wording_carries_the_reset_epoch(self):
        with self.assertRaises(llm_client.ClaudeCliRateLimited) as ctx:
            self._run_once(_envelope("Claude AI usage limit reached|1800000000",
                                     is_error=True))
        self.assertEqual(ctx.exception.resets_at, 1_800_000_000)

    def test_rate_limited_is_never_retried_on_the_backoff_schedule(self):
        exc = llm_client.ClaudeCliRateLimited("window", resets_at=1)
        self.assertFalse(retry.is_retryable(exc))

    def test_rate_limited_is_not_a_login_problem(self):
        # The CLI's own error classifier files "usage limit reached" next to
        # "not logged in"; ours must not, or the summary goes to Gemini.
        with self.assertRaises(llm_client.ClaudeCliRateLimited):
            self._run_once(_envelope("Claude AI usage limit reached|1800000000",
                                     is_error=True))

    def _drive(self, outputs, env=None):
        """summarize_claude_cli against a scripted sequence of CLI outputs."""
        outputs = list(outputs)
        calls = []

        def fake_run(argv, **kwargs):
            calls.append(argv)
            return FakeCompleted(outputs.pop(0))

        with tempfile.TemporaryDirectory() as tmp:
            env_vars = {"MEETING_BOT_ROOT": tmp, "CLAUDE_CLI_BIN": sys.executable,
                        "CLAUDE_CLI_FRAME_VISION": "0"}
            env_vars.update(env or {})
            with mock.patch.dict(os.environ, env_vars), \
                 mock.patch.object(llm_client.subprocess, "run", fake_run):
                out = llm_client.summarize_claude_cli(
                    [], "t", "{transcript}{frame_manifest}")
        return out, calls

    def test_a_hit_window_waits_for_the_reset_then_retries(self):
        reset = int(llm_client.time.time()) + 900
        rejected = self._stream("rejected", reset, result="429", is_error=True,
                                api_error_status=429)
        ok = self._stream("allowed", reset + 18000, result="AFTER RESET")
        events = []
        llm_client.WAIT_HOOK = lambda **kw: events.append(kw)
        out, calls = self._drive([rejected, ok])
        self.assertEqual(out, "AFTER RESET")
        self.assertEqual(len(calls), 2)
        self.assertEqual(len(self.slept), 1)
        # Until the reset plus the margin, give or take the test's own clock.
        self.assertGreater(self.slept[0], 900)
        self.assertLess(self.slept[0], 900 + llm_client.RATE_LIMIT_MARGIN_SECONDS + 5)
        # The hook saw the wait begin and end.
        self.assertEqual(len(events), 2)
        self.assertIsNotNone(events[0]["waiting_until"])
        self.assertEqual(events[0]["window"], "five_hour")
        self.assertIsNone(events[1]["waiting_until"])

    def test_a_reset_beyond_the_cap_gives_up_with_the_time_attached(self):
        reset = int(llm_client.time.time()) + 7 * 3600
        rejected = self._stream("rejected", reset, result="429", is_error=True,
                                api_error_status=429)
        with self.assertRaises(llm_client.ClaudeCliRateLimited) as ctx:
            self._drive([rejected])
        self.assertEqual(ctx.exception.resets_at, reset)
        self.assertEqual(self.slept, [])

    def test_the_cap_is_configurable(self):
        reset = int(llm_client.time.time()) + 900
        rejected = self._stream("rejected", reset, result="429", is_error=True,
                                api_error_status=429)
        with self.assertRaises(llm_client.ClaudeCliRateLimited):
            self._drive([rejected], env={"CLAUDE_CLI_MAX_WAIT_SECONDS": "60"})
        self.assertEqual(self.slept, [])

    def test_no_reset_time_polls_until_the_cap(self):
        env = json.loads(_envelope("API Error: 429", is_error=True))
        env["api_error_status"] = 429
        rejected = json.dumps(env)
        ok = _envelope("EVENTUALLY")
        out, calls = self._drive([rejected, rejected, ok],
                                 env={"CLAUDE_CLI_RATE_LIMIT_POLL_SECONDS": "100",
                                      "CLAUDE_CLI_MAX_WAIT_SECONDS": "250"})
        self.assertEqual(out, "EVENTUALLY")
        self.assertEqual(self.slept, [100, 100])
        # A third refusal would have exceeded the cap and raised.

    def test_the_chain_does_not_advance_to_gemini_on_a_hit_window(self):
        def limited(*a, **k):
            raise llm_client.ClaudeCliRateLimited("window", resets_at=1, window="five_hour")
        with mock.patch.dict(llm_client._BACKENDS,
                             {"claude-cli": limited,
                              "gemini": lambda *a, **k: "FROM GEMINI"}), \
             mock.patch.dict(os.environ,
                             {"SUMMARY_FALLBACK_CHAIN": "claude-cli,gemini"}):
            with self.assertRaises(llm_client.ClaudeCliRateLimited):
                llm_client.summarize_with_fallback([], "t", "{transcript}{frame_manifest}")

    def test_mapreduce_pauses_the_stage_instead_of_merging_around_it(self):
        # Two chunks succeed, one is out of window: the stage must fail
        # (resumable) rather than write a document with a hole in it.
        def fake_summarize(frames, transcript, template, role=None):
            if transcript == "body 1":
                raise llm_client.ClaudeCliRateLimited("window", resets_at=1)
            return "ok"
        chunks = [Chunk(index=i, text=f"body {i}", start_s=i * 60.0,
                        end_s=(i + 1) * 60.0, frames=[]) for i in range(3)]
        with self.assertRaises(llm_client.ClaudeCliRateLimited):
            summarize_chunked(chunks, "PROMPT {transcript}", fake_summarize,
                              log=lambda *a: None)


class FrameThinningTest(unittest.TestCase):
    def _frames(self, kinds):
        return [FrameMeta(timestamp_s=float(i * 10), kind=k, path=f"/f/{i}.jpg",
                          number=i + 1) for i, k in enumerate(kinds)]

    def test_no_cap_returns_everything(self):
        frames = self._frames(["periodic"] * 5)
        self.assertEqual(len(llm_client.thin_frames(frames, 0)), 5)
        self.assertEqual(len(llm_client.thin_frames(frames, 5)), 5)

    def test_scene_changes_are_kept_first(self):
        kinds = ["periodic"] * 8
        kinds[3] = "scene_change"
        kinds[6] = "scene_change"
        kept = llm_client.thin_frames(self._frames(kinds), 4)
        self.assertEqual(len(kept), 4)
        self.assertEqual([f.number for f in kept if f.kind == "scene_change"], [4, 7])

    def test_periodic_frames_cover_the_whole_window(self):
        kept = llm_client.thin_frames(self._frames(["periodic"] * 20), 5)
        self.assertEqual([f.number for f in kept], [1, 6, 11, 15, 20])

    def test_numbers_are_untouched(self):
        # Thinning is about what is offered, never about renumbering: the
        # PDF resolves citations against the whole manifest.
        frames = self._frames(["periodic"] * 9)
        kept = llm_client.thin_frames(frames, 3)
        self.assertEqual([f.number for f in kept], [1, 5, 9])
        self.assertEqual([f.number for f in frames], list(range(1, 10)))

    def test_output_stays_chronological(self):
        kinds = ["periodic"] * 10
        kinds[8] = "scene_change"
        kinds[1] = "scene_change"
        kept = llm_client.thin_frames(self._frames(kinds), 5)
        stamps = [f.timestamp_s for f in kept]
        self.assertEqual(stamps, sorted(stamps))


class ClaudeCliDiscoveryTest(unittest.TestCase):
    def test_missing_cli_is_unavailable_with_install_instructions(self):
        with mock.patch.object(llm_client, "_claude_cli_bin", lambda: None):
            with self.assertRaises(BackendUnavailable) as ctx:
                llm_client.summarize_claude_cli([], "t", "{transcript}{frame_manifest}")
        self.assertIn("install.sh", str(ctx.exception))

    def test_configured_bin_that_does_not_exist_is_ignored(self):
        with mock.patch.dict(os.environ, {"CLAUDE_CLI_BIN": "/nope/claude"}):
            self.assertIsNone(llm_client._claude_cli_bin())


class BackendChainTest(unittest.TestCase):
    def test_legacy_anthropic_name_still_resolves(self):
        # An existing .env whose chain reads `anthropic,gemini` must keep
        # working — there is just no API-key backend behind the name now.
        for alias in ("anthropic", "claude", "claude-cli", "fcc"):
            self.assertIs(llm_client._BACKENDS[alias],
                          llm_client.summarize_claude_cli)

    def test_default_chain_is_cli_then_gemini(self):
        self.assertEqual(llm_client.DEFAULT_FALLBACK_CHAIN, "claude-cli,gemini")

    def test_unavailable_backend_advances_the_chain(self):
        def unavailable(*a, **k):
            raise BackendUnavailable("not logged in")
        with mock.patch.dict(llm_client._BACKENDS,
                             {"claude-cli": unavailable,
                              "gemini": lambda *a, **k: "FROM GEMINI"}), \
             mock.patch.dict(os.environ,
                             {"SUMMARY_FALLBACK_CHAIN": "claude-cli,gemini"}):
            out = llm_client.summarize_with_fallback([], "t", "{transcript}{frame_manifest}")
        self.assertEqual(out, "FROM GEMINI")


class SegmentGranularityTest(unittest.TestCase):
    """Over-long transcript segments are cut before they reach the chunker.

    A segment is the atom for both chunk boundaries and frame windows, so an
    AssemblyAI Thai transcript that comes back as three 30-minute "sentences"
    blew the chunk limit (60k and 70k chars against 40k) and gave every frame
    window a half-hour of slack. Real case: Week01, 3 cues for 2.6 hours.
    """

    def test_a_well_formed_transcript_is_untouched(self):
        segs = [chunking.Segment(0.0, 3.0, "hello"),
                chunking.Segment(3.0, 7.0, "there")]
        self.assertEqual(chunking.split_long_segments(segs), segs)

    def test_a_long_segment_is_split_and_keeps_every_character(self):
        seg = chunking.Segment(0.0, 1782.0, "ก" * 16000)
        out = chunking.split_long_segments([seg])
        self.assertGreater(len(out), 10)
        self.assertEqual("".join(s.text for s in out), seg.text)

    def test_the_pieces_tile_the_original_time_span(self):
        seg = chunking.Segment(100.0, 1000.0, "x" * 9000)
        out = chunking.split_long_segments([seg])
        self.assertAlmostEqual(out[0].start_s, 100.0, places=6)
        self.assertAlmostEqual(out[-1].end_s, 1000.0, places=6)
        for a, b in zip(out, out[1:]):
            self.assertLessEqual(a.end_s, b.start_s + 1e-9)

    def test_no_piece_exceeds_the_duration_cap(self):
        seg = chunking.Segment(0.0, 1800.0, "y" * 20000)
        out = chunking.split_long_segments([seg], max_seconds=120,
                                           max_chars=100000)
        for piece in out:
            self.assertLessEqual(piece.end_s - piece.start_s, 120.5)

    def test_the_char_cap_splits_a_short_but_dense_segment(self):
        seg = chunking.Segment(0.0, 10.0, "z" * 9000)
        out = chunking.split_long_segments([seg], max_seconds=120,
                                           max_chars=2000)
        self.assertGreaterEqual(len(out), 5)

    def test_splitting_prefers_a_word_boundary(self):
        words = ("alpha bravo charlie delta echo foxtrot " * 200).strip()
        out = chunking.split_long_segments([chunking.Segment(0.0, 600.0, words)],
                                           max_seconds=120, max_chars=100000)
        self.assertGreater(len(out), 1)
        # No piece may start or end mid-word.
        for piece in out[:-1]:
            self.assertTrue(piece.text.endswith(" ") or
                            piece.text.rstrip().split()[-1] in words.split())

    def test_zero_disables_the_split(self):
        seg = chunking.Segment(0.0, 5000.0, "q" * 50000)
        self.assertEqual(chunking.split_long_segments([seg], max_seconds=0),
                         [seg])

    def test_chunks_from_giant_segments_respect_the_char_limit(self):
        # Three 30-minute cues, the shape that started this.
        segs = [chunking.Segment(0.0, 1800.0, "a" * 20000),
                chunking.Segment(1800.0, 5400.0, "b" * 60000),
                chunking.Segment(5400.0, 9000.0, "c" * 40000)]
        chunks = chunking.chunk_by_segments(
            chunking.split_long_segments(segs), [], limit=40000, overlap=800)
        for c in chunks:
            self.assertLessEqual(len(c.text), 41000)

    def test_frame_windows_stop_overlapping_once_segments_are_fine(self):
        segs = [chunking.Segment(0.0, 1800.0, "a" * 20000),
                chunking.Segment(1800.0, 5400.0, "b" * 60000)]
        chunks = chunking.chunk_by_segments(
            chunking.split_long_segments(segs), [], limit=40000, overlap=800)
        # Each chunk may reach back only as far as its carried lead-in, not
        # over the whole of the previous chunk.
        for a, b in zip(chunks, chunks[1:]):
            self.assertGreater(b.start_s, a.start_s)


class EdgeFrameTest(unittest.TestCase):
    """A frame no chunk carries is a frame the model can never cite."""

    def segs(self):
        return [chunking.Segment(10.0, 100.0, "a" * 100),
                chunking.Segment(100.0, 200.0, "b" * 100)]

    def frames(self, *stamps):
        return [llm_client.FrameMeta(timestamp_s=t, kind="periodic",
                                     path=f"/f/{t}.jpg") for t in stamps]

    def test_a_frame_before_the_first_word_reaches_the_first_chunk(self):
        # Real case: the frame at t=0 with segment 1 starting at 0.3s.
        chunks = chunking.chunk_by_segments(self.segs(), self.frames(0.0),
                                            limit=150, overlap=0)
        self.assertIn(0.0, [f.timestamp_s for f in chunks[0].frames])

    def test_a_frame_after_the_last_word_reaches_the_last_chunk(self):
        chunks = chunking.chunk_by_segments(self.segs(), self.frames(999.0),
                                            limit=150, overlap=0)
        self.assertIn(999.0, [f.timestamp_s for f in chunks[-1].frames])

    def test_every_frame_lands_in_at_least_one_chunk(self):
        frames = self.frames(0.0, 50.0, 150.0, 999.0)
        chunks = chunking.chunk_by_segments(self.segs(), frames,
                                            limit=150, overlap=0)
        carried = {f.timestamp_s for c in chunks for f in c.frames}
        self.assertEqual(carried, {0.0, 50.0, 150.0, 999.0})


class FrameNumberingTest(unittest.TestCase):
    """Frame numbers must mean the same thing to the model and to the PDF.

    Regression: _render numbers whatever list it is given and is called once
    per chunk, so every chunk announced its own frames as 1..N. The model
    cited them correctly; the PDF resolved them against the whole recording
    and printed unrelated pictures. Seen in a real summary as "Frame 4" cited
    at both 219s and 5484s.
    """

    def frames(self, *stamps):
        return [llm_client.FrameMeta(timestamp_s=t, kind="periodic",
                                     path=f"/f/{t}.jpg") for t in stamps]

    def test_numbers_are_assigned_across_the_whole_manifest(self):
        frames = llm_client.assign_numbers(self.frames(30.0, 10.0, 20.0))
        self.assertEqual([f.number for f in frames], [1, 2, 3])
        self.assertEqual([f.timestamp_s for f in frames], [10.0, 20.0, 30.0])

    def test_a_later_chunk_keeps_the_recordings_numbers(self):
        all_frames = llm_client.assign_numbers(
            self.frames(10.0, 20.0, 30.0, 40.0))
        tail = all_frames[2:]          # what chunk 2 would be handed
        _, text = llm_client._render(tail, "transcript", "{frame_manifest}")
        self.assertIn("[frame 3 @ 30.0s", text)
        self.assertIn("[frame 4 @ 40.0s", text)
        self.assertNotIn("[frame 1 @", text)

    def test_unnumbered_frames_still_number_from_one(self):
        # A caller that never called assign_numbers (the older behaviour, and
        # what the unit tests below rely on) must be unaffected.
        _, text = llm_client._render(self.frames(5.0), "t", "{frame_manifest}")
        self.assertIn("[frame 1 @ 5.0s", text)


# ---------------------------------------------------------------------------
# The cache-stable prefix
#
# Claude caches an exact prefix. The claude-cli backend therefore has to send
# the unchanging instructions as a system prompt read from a stable file, and
# keep everything that varies — the chunk label, the reference material, the
# transcript, the frame paths — in the piped user turn. These tests are what
# stop something varying from drifting back into the static half, which fails
# silently: the summary is fine, the cache simply never hits.
# ---------------------------------------------------------------------------

BEGIN = llm_client.STATIC_PROMPT_BEGIN
END = llm_client.STATIC_PROMPT_END

MARKED_TEMPLATE = (
    f"{BEGIN}\n<instructions>\nAlways cite frames.\n</instructions>\n{END}\n\n"
    "# Input\n\n<transcript>\n{transcript}\n</transcript>\n"
    "<frames>\n{frame_manifest}\n</frames>\n"
)


class StaticPromptSplitTest(unittest.TestCase):
    def test_an_unmarked_template_is_returned_untouched(self):
        # Every prompt file older than summarize-v2.md takes this path, and
        # must behave exactly as it did before.
        template = "Do the thing.\n{transcript}\n{frame_manifest}"
        static, dynamic = llm_client.split_static_prompt(template)
        self.assertIsNone(static)
        self.assertEqual(dynamic, template)

    def test_the_marked_block_is_lifted_out(self):
        static, dynamic = llm_client.split_static_prompt(MARKED_TEMPLATE)
        self.assertIn("Always cite frames.", static)
        self.assertNotIn("Always cite frames.", dynamic)
        self.assertIn("{transcript}", dynamic)
        self.assertIn("{frame_manifest}", dynamic)

    def test_a_prepended_chunk_label_stays_dynamic(self):
        # mapreduce prepends the part label to the template. If that landed in
        # the static half, every chunk would write a different system prompt
        # file and nothing would ever be cached.
        template = "You are summarizing PART 2 OF 5.\n\n" + MARKED_TEMPLATE
        static, dynamic = llm_client.split_static_prompt(template)
        self.assertNotIn("PART 2 OF 5", static)
        self.assertIn("PART 2 OF 5", dynamic)

    def test_appended_reference_material_stays_dynamic(self):
        # summarize.py appends the lecturer's slides to the template; they
        # differ per run.
        template = MARKED_TEMPLATE + "\n## Reference material\nWeek 4 slides."
        static, dynamic = llm_client.split_static_prompt(template)
        self.assertNotIn("Week 4 slides.", static)
        self.assertIn("Week 4 slides.", dynamic)

    def test_an_empty_marked_block_falls_back(self):
        template = f"{BEGIN}\n\n{END}\n{{transcript}}{{frame_manifest}}"
        static, _ = llm_client.split_static_prompt(template)
        self.assertIsNone(static)

    def test_markers_never_reach_a_model(self):
        # gemini renders the template whole; the delimiters are structure for
        # us and noise for the model.
        _, text = llm_client._render([], "T", MARKED_TEMPLATE)
        self.assertNotIn(BEGIN, text)
        self.assertNotIn(END, text)
        self.assertIn("Always cite frames.", text)


class ShippedMarkedPromptsTest(unittest.TestCase):
    """The real prompt files that carry the markers, split correctly.

    A placeholder drifting above the `end` marker would put the transcript in
    the cached prefix — a KeyError at best, a cache that never hits at worst.
    """

    PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"

    def _marked(self):
        found = []
        for path in sorted(self.PROMPTS_DIR.glob("*.md")):
            text = path.read_text()
            if llm_client.STATIC_PROMPT_BEGIN in text:
                found.append((path, text))
        return found

    def test_the_expected_files_carry_the_markers(self):
        names = {path.name for path, _ in self._marked()}
        self.assertIn("summarize-v2.md", names)
        self.assertIn("lecture-claude.md", names)

    def test_each_splits_cleanly(self):
        for path, text in self._marked():
            with self.subTest(prompt=path.name):
                static, dynamic = llm_client.split_static_prompt(text)
                self.assertIsNotNone(static, "markers present but split failed")
                # The placeholders belong to the per-call half, always.
                for placeholder in ("{transcript}", "{frame_manifest}"):
                    self.assertNotIn(placeholder, static)
                    self.assertIn(placeholder, dynamic)
                # And the static half must survive not being .format()ed: any
                # other brace in it would have raised before the split existed.
                self.assertGreater(len(static), 200)

    def test_the_role_line_is_in_the_cached_half(self):
        # lecture-claude.md used to lose it: load_prompt_template cut at the
        # first "# Input", which matched the "# Input Data" heading near the
        # top, so the prompt began with the orphaned word "Data".
        text = (self.PROMPTS_DIR / "lecture-claude.md").read_text()
        static, _ = llm_client.split_static_prompt(text)
        self.assertTrue(static.startswith("You are an expert academic tutor"))

    def test_load_prompt_template_keeps_a_marked_file_whole(self):
        import summarize as summarize_main
        template = summarize_main.load_prompt_template(
            self.PROMPTS_DIR / "lecture-claude.md")
        self.assertIn("You are an expert academic tutor", template)
        self.assertIn("# Execution Rules", template)
        self.assertIn("{transcript}", template)


class StaticPromptInvocationTest(unittest.TestCase):
    """What the CLI is actually handed."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.calls = []

    def _run(self, transcript="Dijkstra.", template=MARKED_TEMPLATE, env=None):
        def fake_run(argv, **kwargs):
            self.calls.append((argv, kwargs))
            return FakeCompleted(stdout=_envelope("BODY"))
        environ = {"MEETING_BOT_ROOT": self.tmp.name,
                   "CLAUDE_CLI_BIN": sys.executable}
        environ.update(env or {})
        with mock.patch.dict(os.environ, environ), \
             mock.patch.object(llm_client.subprocess, "run", fake_run):
            llm_client.summarize_claude_cli([], transcript, template)
        return self.calls[-1]

    def _flag(self, argv, flag):
        return argv[argv.index(flag) + 1] if flag in argv else None

    def test_the_static_half_goes_in_as_a_system_prompt_file(self):
        argv, kwargs = self._run()
        path = self._flag(argv, "--append-system-prompt-file")
        self.assertIsNotNone(path)
        self.assertIn("Always cite frames.", Path(path).read_text())
        # ...and out of the piped prompt, or it would be sent twice.
        self.assertNotIn("Always cite frames.", kwargs["input"])

    def test_dynamic_system_prompt_sections_are_excluded(self):
        # The CLI's own system prompt carries cwd, env info and the date. Left
        # in front of ours it changes daily and nothing behind it can cache.
        argv, _ = self._run()
        self.assertIn("--exclude-dynamic-system-prompt-sections", argv)

    def test_the_path_is_stable_across_calls_that_differ(self):
        first, _ = self._run(transcript="lecture one")
        second, _ = self._run(transcript="a completely different lecture")
        self.assertEqual(self._flag(first, "--append-system-prompt-file"),
                         self._flag(second, "--append-system-prompt-file"))

    def test_a_different_template_gets_a_different_file(self):
        first, _ = self._run()
        other = MARKED_TEMPLATE.replace("Always cite frames.", "Never guess.")
        second, _ = self._run(template=other)
        self.assertNotEqual(self._flag(first, "--append-system-prompt-file"),
                            self._flag(second, "--append-system-prompt-file"))

    def test_the_transcript_still_travels_on_stdin(self):
        argv, kwargs = self._run(transcript="Dijkstra runs in O(E log V).")
        self.assertIn("Dijkstra runs in O(E log V).", kwargs["input"])
        self.assertNotIn("Dijkstra", " ".join(argv))

    def test_the_toggle_reverts_to_the_inline_prompt(self):
        argv, kwargs = self._run(env={"CLAUDE_CLI_STATIC_PROMPT": "0"})
        self.assertNotIn("--append-system-prompt-file", argv)
        self.assertNotIn("--exclude-dynamic-system-prompt-sections", argv)
        self.assertIn("Always cite frames.", kwargs["input"])

    def test_an_unmarked_template_adds_no_flags(self):
        argv, kwargs = self._run(template="Old style.\n{transcript}{frame_manifest}")
        self.assertNotIn("--append-system-prompt-file", argv)
        self.assertIn("Old style.", kwargs["input"])


def _slide_frame(path, size=(1920, 1080), slide=None, seed=0):
    """A synthetic keyframe: dark UI with a bright slide carrying some marks.

    Not solid white — is_blank() would (correctly) drop that — and with a
    few dark shapes on the slide so two frames with different `seed`s hash
    apart while two with the same seed hash together.
    """
    from PIL import Image, ImageDraw
    img = Image.new("RGB", size, (20, 20, 20))
    draw = ImageDraw.Draw(img)
    if slide is None:
        slide = (0, 0, size[0], size[1])
    draw.rectangle(slide, fill=(245, 245, 245))
    left, top, right, bottom = slide
    w, h = right - left, bottom - top
    # A title bar plus "text lines" whose lengths depend on the seed — the
    # shape a real slide has, and what tells two slides of one deck apart.
    draw.rectangle((left + w // 20, top + h // 12, right - w // 20, top + h // 5),
                   fill=(40, 40, 40))
    for i in range(6):
        y = top + h // 3 + i * (h // 10)
        length = w * (3 + (i * 5 + seed * 7) % 6) / 10
        draw.rectangle((left + w // 20, y, left + w // 20 + int(length), y + h // 24),
                       fill=(30, 30, 30))
    img.save(path, "JPEG", quality=90)
    return path


class FrameDownscaleTest(unittest.TestCase):
    """A 1920x1080 keyframe costs ~1,844 tokens every time the model sees it.

    Driven on the Read path (CLAUDE_CLI_FRAME_INLINE=0) so the path the model
    is pointed at is visible in the prompt; the copies on disk are the same
    either way, and the inline test below checks the bytes that ride along.
    """

    def setUp(self):
        try:
            from PIL import Image  # noqa: F401
        except ImportError:
            self.skipTest("Pillow not installed")
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.frame_dir = Path(self.tmp.name) / "frames" / "run_1"
        self.frame_dir.mkdir(parents=True)
        self.original = _slide_frame(self.frame_dir / "scene_00001.jpg")
        self.before = self.original.read_bytes()
        self.frames = [FrameMeta(timestamp_s=1.0, kind="scene_change",
                                 path=str(self.original), number=1)]
        self.calls = []

    def _run(self, env=None):
        def fake_run(argv, **kwargs):
            self.calls.append((argv, kwargs))
            return FakeCompleted(stdout=_envelope("BODY"))
        environ = {"MEETING_BOT_ROOT": self.tmp.name,
                   "CLAUDE_CLI_BIN": sys.executable,
                   "CLAUDE_CLI_FRAME_INLINE": "0",
                   "PDF_FRAME_CROP": "none"}
        environ.update(env or {})
        with mock.patch.dict(os.environ, environ), \
             mock.patch.object(llm_client.subprocess, "run", fake_run):
            llm_client.summarize_claude_cli(
                self.frames, "t", "{transcript}\n{frame_manifest}")
        return self.calls[-1]

    def _sent_path(self, kwargs):
        for token in kwargs["input"].split():
            if token.endswith(".jpg"):
                return Path(token)
        return None

    def test_the_model_is_pointed_at_a_downscaled_copy(self):
        from PIL import Image
        _, kwargs = self._run()
        sent = self._sent_path(kwargs)
        self.assertNotEqual(sent, self.original)
        with Image.open(sent) as img:
            self.assertEqual(max(img.size), 768)

    def test_the_saved_frame_is_left_alone(self):
        # pdf.py crops and embeds the original; it needs the full resolution.
        self._run()
        self.assertEqual(self.original.read_bytes(), self.before)

    def test_the_dimension_is_configurable(self):
        from PIL import Image
        _, kwargs = self._run(env={"FRAME_MAX_DIMENSION": "512"})
        with Image.open(self._sent_path(kwargs)) as img:
            self.assertEqual(max(img.size), 512)

    def test_zero_disables_the_downscale(self):
        _, kwargs = self._run(env={"FRAME_MAX_DIMENSION": "0"})
        self.assertEqual(self._sent_path(kwargs), self.original)

    def test_a_frame_already_small_enough_is_sent_as_is(self):
        small = _slide_frame(self.frame_dir / "scene_00002.jpg", size=(640, 360))
        self.frames = [FrameMeta(timestamp_s=1.0, kind="periodic",
                                 path=str(small), number=1)]
        _, kwargs = self._run()
        self.assertEqual(self._sent_path(kwargs), small)

    def test_add_dir_still_covers_the_original_frame_directory(self):
        argv, _ = self._run()
        dirs = [argv[i + 1] for i, a in enumerate(argv) if a == "--add-dir"]
        self.assertIn(str(self.frame_dir), dirs)
        for directory in dirs:
            self.assertTrue(str(directory).startswith(str(self.frame_dir)))

    def test_a_second_run_reuses_the_copy(self):
        _, first = self._run()
        sent = self._sent_path(first)
        stamp = sent.stat().st_mtime_ns
        _, second = self._run()
        self.assertEqual(self._sent_path(second), sent)
        self.assertEqual(sent.stat().st_mtime_ns, stamp)

    def test_inline_delivery_sends_the_downscaled_bytes(self):
        # Same copy, different transport: the image block carries the small
        # file, not the 1920x1080 original.
        import base64
        from PIL import Image
        import io
        _, kwargs = self._run(env={"CLAUDE_CLI_FRAME_INLINE": "1"})
        _, images = _unpack_stdin(kwargs["input"])
        self.assertEqual(len(images), 1)
        data = base64.b64decode(images[0]["source"]["data"])
        self.assertLess(len(data), len(self.before))
        with Image.open(io.BytesIO(data)) as img:
            self.assertEqual(max(img.size), 768)


def _bright_fraction(img):
    gray = img.convert("L").resize((50, 28))
    values = list(gray.getdata())
    return sum(1 for v in values if v > 200) / len(values)


class FrameCropForLlmTest(unittest.TestCase):
    """The copy the model sees is cropped to the slide before it is shrunk.

    At 768px a whole 1920x1080 frame gives the slide maybe 500px of width;
    cropped first, the slide gets all 768. Same detector, same mode
    (PDF_FRAME_CROP) as the PDF, so the model looks at the picture the PDF
    prints — and framecrop declines rather than guesses, so a frame with no
    findable slide is shrunk whole.
    """

    def setUp(self):
        try:
            from PIL import Image  # noqa: F401
        except ImportError:
            self.skipTest("Pillow not installed")
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.frame_dir = Path(self.tmp.name) / "frames" / "run_1"
        self.frame_dir.mkdir(parents=True)
        # A 1280x720 slide sitting in dark chrome at (320, 180).
        self.original = _slide_frame(self.frame_dir / "scene_00001.jpg",
                                     slide=(320, 180, 1600, 900))

    def test_slide_mode_crops_then_fits(self):
        import framecrop
        from PIL import Image
        dst = self.frame_dir / "llm" / "scene_00001.jpg"
        out = framecrop.fit_for_llm(self.original, dst, mode="slide", max_dim=768)
        self.assertEqual(out, dst)
        with Image.open(dst) as img:
            self.assertEqual(img.width, 768)
            # The slide, not the frame: the dark chrome is gone, so the
            # copy is markedly lighter than the whole-frame version below.
            self.assertGreater(_bright_fraction(img), 0.6)
            self.assertLess(abs(img.height - 432), 16)   # still ~16:9

    def test_none_mode_only_downscales(self):
        import framecrop
        from PIL import Image
        dst = self.frame_dir / "llm" / "scene_00001.jpg"
        framecrop.fit_for_llm(self.original, dst, mode="none", max_dim=768)
        with Image.open(dst) as img:
            self.assertEqual(img.size, (768, 432))
            self.assertLess(_bright_fraction(img), 0.45)   # chrome still there

    def test_nothing_to_do_returns_the_source(self):
        import framecrop
        small = _slide_frame(self.frame_dir / "small.jpg", size=(640, 360))
        dst = self.frame_dir / "llm" / "small.jpg"
        out = framecrop.fit_for_llm(small, dst, mode="none", max_dim=768)
        self.assertEqual(out, small)
        self.assertFalse(dst.exists())

    def test_the_copy_directory_names_the_crop_mode(self):
        # So flipping PDF_FRAME_CROP doesn't reuse copies cut the old way.
        src = Path("/x/frames/run/f.jpg")
        self.assertEqual(llm_client._llm_copy_path(src, 768, "slide"),
                         Path("/x/frames/run/llm-768-slide/f.jpg"))
        self.assertEqual(llm_client._llm_copy_path(src, 768, "none"),
                         Path("/x/frames/run/llm-768/f.jpg"))

    def test_the_backend_uses_pdf_frame_crop(self):
        calls = []

        def fake_run(argv, **kwargs):
            calls.append((argv, kwargs))
            return FakeCompleted(stdout=_envelope("BODY"))
        frames = [FrameMeta(timestamp_s=1.0, kind="scene_change",
                            path=str(self.original), number=1)]
        env = {"MEETING_BOT_ROOT": self.tmp.name, "CLAUDE_CLI_BIN": sys.executable,
               "CLAUDE_CLI_FRAME_INLINE": "0", "PDF_FRAME_CROP": "slide"}
        with mock.patch.dict(os.environ, env), \
             mock.patch.object(llm_client.subprocess, "run", fake_run):
            llm_client.summarize_claude_cli(frames, "t", "{transcript}\n{frame_manifest}")
        prompt = calls[0][1]["input"]
        self.assertIn("/llm-768-slide/scene_00001.jpg", prompt)


class DropUninformativeTest(unittest.TestCase):
    """Blank frames and repeats of the same slide are not offered."""

    def setUp(self):
        try:
            from PIL import Image  # noqa: F401
        except ImportError:
            self.skipTest("Pillow not installed")
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def _frame(self, name, ts, seed, kind="periodic", part=0, blank=False):
        from PIL import Image
        path = self.dir / f"{name}.jpg"
        if blank:
            Image.new("RGB", (640, 360), "black").save(path)
        else:
            _slide_frame(path, size=(640, 360), seed=seed)
        return FrameMeta(timestamp_s=ts, kind=kind, path=str(path), part=part)

    def test_blank_frames_are_dropped(self):
        frames = [self._frame("a", 0, 1), self._frame("b", 30, 0, blank=True),
                  self._frame("c", 60, 2)]
        kept, blanks, dupes = llm_client.drop_uninformative(frames)
        self.assertEqual([f.timestamp_s for f in kept], [0, 60])
        self.assertEqual((blanks, dupes), (1, 0))

    def test_consecutive_repeats_of_one_slide_are_dropped(self):
        frames = [self._frame("a", 0, 1), self._frame("b", 30, 1),
                  self._frame("c", 60, 1), self._frame("d", 90, 2)]
        kept, blanks, dupes = llm_client.drop_uninformative(frames)
        self.assertEqual([f.timestamp_s for f in kept], [0, 90])
        self.assertEqual((blanks, dupes), (0, 2))

    def test_a_slide_returned_to_later_is_kept(self):
        # Only *consecutive* repeats go: coming back to a diagram twenty
        # minutes later is a moment the notes may cite.
        frames = [self._frame("a", 0, 1), self._frame("b", 30, 2),
                  self._frame("c", 60, 1)]
        kept, _, dupes = llm_client.drop_uninformative(frames)
        self.assertEqual(len(kept), 3)
        self.assertEqual(dupes, 0)

    def test_numbers_are_untouched(self):
        frames = llm_client.assign_numbers(
            [self._frame("a", 0, 1), self._frame("b", 30, 1), self._frame("c", 60, 2)])
        kept, _, _ = llm_client.drop_uninformative(frames)
        self.assertEqual([f.number for f in kept], [1, 3])

    def test_frames_of_different_videos_are_never_each_others_repeat(self):
        frames = [self._frame("a", 0, 1, part=1), self._frame("b", 0, 1, part=2)]
        kept, _, dupes = llm_client.drop_uninformative(frames)
        self.assertEqual(len(kept), 2)
        self.assertEqual(dupes, 0)

    def test_dedupe_can_be_turned_off(self):
        frames = [self._frame("a", 0, 1), self._frame("b", 30, 1)]
        with mock.patch.dict(os.environ, {"CLAUDE_CLI_FRAME_DEDUPE": "0"}):
            kept, _, dupes = llm_client.drop_uninformative(frames)
        self.assertEqual(len(kept), 2)

    def test_dedupe_runs_before_the_cap(self):
        # Ten periodic samples of one slide, then one new slide, cap 3: the
        # model sees both slides. Thinning first would have shown it the
        # first slide three times.
        frames = [self._frame(f"f{i}", i * 30, 1) for i in range(10)]
        frames.append(self._frame("new", 300, 2))
        frames = llm_client.assign_numbers(frames)
        calls = []

        def fake_run(argv, **kwargs):
            calls.append(kwargs)
            return FakeCompleted(stdout=_envelope("BODY"))
        env = {"MEETING_BOT_ROOT": self.tmp.name, "CLAUDE_CLI_BIN": sys.executable,
               "CLAUDE_CLI_MAX_FRAMES": "3", "PDF_FRAME_CROP": "none"}
        with mock.patch.dict(os.environ, env), \
             mock.patch.object(llm_client.subprocess, "run", fake_run):
            llm_client.summarize_claude_cli(frames, "t", "{transcript}\n{frame_manifest}")
        text, images = _unpack_stdin(calls[0]["input"])
        self.assertEqual(len(images), 2)
        self.assertIn("[frame 1 @ 0.0s", text)
        self.assertIn("[frame 11 @ 300.0s", text)

    def test_frame_hash_tells_slides_apart(self):
        import framecrop
        a = framecrop.frame_hash(self._frame("a", 0, 1).path)
        b = framecrop.frame_hash(self._frame("b", 0, 1).path)
        c = framecrop.frame_hash(self._frame("c", 0, 2).path)
        self.assertEqual(framecrop.hamming(a, b), 0)
        self.assertGreater(framecrop.hamming(a, c), llm_client.FRAME_DEDUPE_MAX_DISTANCE)

    def _text_slide(self, name, title, cursor=None, tile=False):
        # Real rendered text in dark chrome, the way a Meet recording of a
        # shared slide looks: this is the case the hash exists for.
        from PIL import Image, ImageDraw
        img = Image.new("RGB", (1920, 1080), (20, 20, 20))
        draw = ImageDraw.Draw(img)
        draw.rectangle((320, 180, 1600, 900), fill="white")
        draw.text((400, 300), title, fill="black", font_size=60)
        if cursor:
            draw.ellipse((cursor[0], cursor[1], cursor[0] + 18, cursor[1] + 18), fill="black")
        if tile:
            draw.rectangle((1650, 200, 1900, 340), fill=(90, 120, 200))
        path = self.dir / f"{name}.jpg"
        img.save(path, "JPEG", quality=90)
        return path

    def test_a_changed_title_is_a_different_slide(self):
        # The false positive seen live 2026-09-12: same template, different
        # title, deduped by an average hash. Texture bits tell them apart.
        import framecrop
        a = framecrop.frame_hash(self._text_slide("t1", "Dijkstra: relax edges"))
        b = framecrop.frame_hash(self._text_slide("t2", "Bellman-Ford: V-1 passes"))
        self.assertGreater(framecrop.hamming(a, b), llm_client.FRAME_DEDUPE_MAX_DISTANCE)

    def test_a_cursor_or_a_filmstrip_change_is_the_same_slide(self):
        import framecrop
        a = framecrop.frame_hash(self._text_slide("s1", "Dijkstra: relax edges"))
        cursor = framecrop.frame_hash(self._text_slide("s2", "Dijkstra: relax edges", cursor=(900, 600)))
        tile = framecrop.frame_hash(self._text_slide("s3", "Dijkstra: relax edges", tile=True))
        self.assertLessEqual(framecrop.hamming(a, cursor), llm_client.FRAME_DEDUPE_MAX_DISTANCE)
        self.assertLessEqual(framecrop.hamming(a, tile), llm_client.FRAME_DEDUPE_MAX_DISTANCE)


class PromptOrderForCachingTest(unittest.TestCase):
    """What varies per chunk goes last; what is the same for a run goes first."""

    def test_the_chunk_label_is_appended_not_prepended(self):
        seen = []

        def fake_summarize(frames, transcript, template, role=None):
            seen.append(template)
            return "x"
        chunks = [Chunk(index=i, text=f"body {i}", start_s=i * 60.0,
                        end_s=(i + 1) * 60.0, frames=[]) for i in range(2)]
        summarize_chunked(chunks, "INSTRUCTIONS\n{transcript}\n{frame_manifest}",
                          fake_summarize, log=lambda *a: None)
        chunk_templates = [t for t in seen if "ONE PART" in t]
        self.assertEqual(len(chunk_templates), 2)
        for template in chunk_templates:
            self.assertTrue(template.startswith("INSTRUCTIONS"))
            self.assertGreater(template.index("ONE PART"), template.index("{frame_manifest}"))

    def test_the_merge_is_called_with_the_merge_role(self):
        roles = []

        def fake_summarize(frames, transcript, template, role=None):
            roles.append(role)
            return "x"
        chunks = [Chunk(index=i, text=f"body {i}", start_s=i * 60.0,
                        end_s=(i + 1) * 60.0, frames=[]) for i in range(2)]
        summarize_chunked(chunks, "P {transcript}", fake_summarize, log=lambda *a: None)
        self.assertEqual(roles, [None, None, "merge"])

    def test_reference_material_goes_to_the_top_of_the_dynamic_half(self):
        import summarize as summarize_main

        class Bundle:
            def text_block(self):
                return "Week 4 slides {x}"

            def provenance(self):
                return "repo"
        template = (f"{BEGIN}\nRULES\n{END}\n# Input\n{{transcript}}\n{{frame_manifest}}\n")
        out = summarize_main.inject_resources(template, Bundle())
        static, dynamic = llm_client.split_static_prompt(out)
        self.assertNotIn("Week 4", static)
        self.assertLess(dynamic.index("Week 4 slides {{x}}"), dynamic.index("# Input"))

    def test_reference_material_is_appended_to_an_unmarked_template(self):
        import summarize as summarize_main

        class Bundle:
            def text_block(self):
                return "Week 4 slides"

            def provenance(self):
                return "repo"
        out = summarize_main.inject_resources("RULES\n{transcript}", Bundle())
        self.assertTrue(out.startswith("RULES"))
        self.assertGreater(out.index("Week 4"), out.index("{transcript}"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
