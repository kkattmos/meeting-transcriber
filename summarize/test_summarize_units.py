#!/usr/bin/env python3
"""
Unit tests for the parts of the summarize stage that don't need an API key:
retry classification/backoff, transcript chunking, map-reduce, and the document
wrapper.

    python3 summarize/test_summarize_units.py

The LLM itself is stubbed, so this exercises our logic (what counts as
retryable, how chunks are cut, what the final markdown looks like) rather than
any provider's behaviour.
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import chunking  # noqa: E402
import document  # noqa: E402
import retry  # noqa: E402
from chunking import Chunk, Segment  # noqa: E402
from llm_client import FrameMeta  # noqa: E402
from mapreduce import summarize_chunked  # noqa: E402


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

        def fake_summarize(frames, transcript, template):
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
        def fake_summarize(frames, transcript, template):
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
        def fake_summarize(frames, transcript, template):
            raise RuntimeError("everything is down")

        with self.assertRaises(RuntimeError):
            summarize_chunked(self._chunks(2), "P {transcript}",
                              fake_summarize, log=lambda *a: None)

    def test_chunk_order_is_preserved_despite_parallelism(self):
        import time

        def fake_summarize(frames, transcript, template):
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

    def test_local_file_source_label(self):
        out = document.build_document(
            "body", source="/opt/meeting-bot/recordings/a.mp4",
            source_kind="local_file", title="A", transcript="x")
        self.assertIn("Source File: `/opt/meeting-bot/recordings/a.mp4`", out)
        self.assertNotIn("Youtube Link", out)

    def test_combine_puts_the_chapter_line_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = []
            for i in range(3):
                p = Path(tmp) / f"{i}.md"
                p.write_text(document.build_document(
                    f"body {i}", source=f"https://youtu.be/v{i}",
                    source_kind="youtube", title=f"Video {i}", transcript="t"))
                paths.append(p)
            combined = document.combine_documents(paths)
        self.assertEqual(combined.count(document.CHAPTER_PLACEHOLDER), 1)
        self.assertEqual(combined.count("<!-- meeting-transcriber"), 0)
        for i in range(3):
            self.assertIn(f"# Video {i}", combined)
        # Input order is preserved, which is what makes it drop-in.
        self.assertLess(combined.index("# Video 0"), combined.index("# Video 1"))
        self.assertLess(combined.index("# Video 1"), combined.index("# Video 2"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
