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
        return self.calls[0][1]["input"]

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

    def test_print_mode_and_json_output(self):
        self._run()
        argv = self._argv()
        self.assertIn("-p", argv)
        self.assertEqual(argv[argv.index("--output-format") + 1], "json")

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

    def test_vision_grants_read_scoped_to_the_frame_dir(self):
        self._run()
        argv = self._argv()
        self.assertEqual(argv[argv.index("--tools") + 1], "Read")
        self.assertEqual(argv[argv.index("--allowedTools") + 1], "Read")
        self.assertEqual(argv[argv.index("--add-dir") + 1], str(self.frame_dir))

    def test_vision_puts_absolute_frame_paths_in_the_prompt(self):
        self._run()
        prompt = self._prompt()
        for frame in self.frames:
            self.assertIn(str(Path(frame.path).resolve()), prompt)
        self.assertIn("Read tool", prompt)

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


if __name__ == "__main__":
    unittest.main(verbosity=2)
