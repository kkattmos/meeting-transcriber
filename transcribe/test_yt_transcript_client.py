#!/usr/bin/env python3
"""
Unit tests for yt_transcript_client — offline, mocked HTTP.

These tests pin down the exact wire format `_call_api` must produce so we
can confirm a refactor (e.g. urllib -> requests) doesn't drift the URL,
headers, body, or timeout. They mock requests.post via unittest.mock
and never touch the network.

Run from the project root:
    python3 -m unittest transcribe.test_yt_transcript_client -v
or directly:
    python3 transcribe/test_yt_transcript_client.py
"""
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

# Make the module importable when this file is run directly.
sys.path.insert(0, str(Path(__file__).resolve().parent))

import yt_transcript_client as ytc


class FakeResponse:
    """Minimal stand-in for requests.Response.

    Only the attributes _call_api touches: status_code, reason, json(),
    text, raise_for_status(). The HTTP status 'reason phrase' is what
    urllib called .reason on the HTTPError object; requests puts it on
    the response directly.
    """

    REASONS = {
        200: "OK",
        401: "Unauthorized",
        403: "Forbidden",
        404: "Not Found",
        429: "Too Many Requests",
        500: "Internal Server Error",
        503: "Service Unavailable",
    }

    def __init__(self, status_code, body=None, text=""):
        self.status_code = status_code
        self.reason = self.REASONS.get(status_code, "Unknown")
        self._body = body if body is not None else {}
        self.text = text or json.dumps(self._body)

    def json(self):
        return self._body

    def raise_for_status(self):
        # Mirror requests.Response.raise_for_status() — we don't call this
        # from _call_api today, but include it for future-proofing the
        # mock shape.
        if self.status_code >= 400:
            import requests
            raise requests.HTTPError(
                f"{self.status_code} Server Error: {self.reason}",
                response=self,
            )


class CallApiWireFormatTest(unittest.TestCase):
    """Verify the exact URL, headers, body, and timeout `_call_api` sends."""

    @patch("yt_transcript_client.requests.post")
    def test_successful_post_returns_parsed_json(self, mock_post):
        """200 with a non-empty body: _call_api returns parsed JSON and
        passes URL/headers/body/timeout to requests.post verbatim."""
        expected_body = [
            {
                "id": "jNQXAC9IVRw",
                "transcripts": [
                    {"text": "hello", "offset": 0, "duration": 1.0},
                ],
            }
        ]
        mock_post.return_value = FakeResponse(200, body=expected_body)

        result = ytc._call_api("jNQXAC9IVRw", "test-token-123")

        # URL — exactly the documented endpoint, no trailing slash drift.
        args, kwargs = mock_post.call_args
        self.assertEqual(args[0], ytc.API_URL)
        self.assertFalse(args[1:], "positional `data`/`json` should be kwargs")

        # Headers — Basic auth with the supplied token.
        self.assertEqual(kwargs["headers"]["Authorization"], "Basic test-token-123")
        self.assertEqual(kwargs["headers"]["Content-Type"], "application/json")

        # Body — {"ids": [<video_id>]} passed via the json= kwarg so
        # requests handles encoding itself.
        self.assertEqual(kwargs["json"], {"ids": ["jNQXAC9IVRw"]})
        self.assertNotIn("data", kwargs, "should not also pass raw `data`")

        # Timeout — the same constant the rest of the module reads.
        self.assertEqual(kwargs["timeout"], ytc.HTTP_TIMEOUT_SECONDS)

        # Return value — parsed segments dict, identical to what the
        # urllib version returned.
        self.assertEqual(result, expected_body)

    @patch("yt_transcript_client.requests.post")
    def test_retryable_status_raises_runtime_error(self, mock_post):
        """HTTP 429 must raise RuntimeError (caller advances to next key).

        This is the exact behaviour that the urllib path produced, and
        fetch_transcript relies on it to drive round-robin key failover.
        """
        mock_post.return_value = FakeResponse(
            429, text="rate limited", body={}
        )

        with self.assertRaises(RuntimeError) as cm:
            ytc._call_api("jNQXAC9IVRw", "bad-key")
        # Message shape — should mention HTTP 429 so the stderr log line
        # in fetch_transcript is informative.
        self.assertIn("429", str(cm.exception))

    @patch("yt_transcript_client.requests.post")
    def test_network_error_propagates(self, mock_post):
        """A requests.exceptions.RequestException must propagate, not be
        swallowed into RuntimeError. fetch_transcript catches
        RequestException separately and logs 'network error'."""
        import requests
        mock_post.side_effect = requests.exceptions.ConnectionError(
            "DNS lookup failed"
        )

        with self.assertRaises(requests.exceptions.RequestException):
            ytc._call_api("jNQXAC9IVRw", "any-key")


class TracksShapeTest(unittest.TestCase):
    """The live API shape (verified 2026-09), where timing lives.

    A real response is one entry per video carrying a flat `text` string
    plus `tracks: [{language, transcript: [{start, dur, text}, ...]}]`.
    Before `_pick_track` existed, the parser fell through to the entry dict
    itself and produced a SINGLE segment holding the whole transcript with
    offset 0 and duration 0 — which silently destroyed every timestamp the
    chunker uses to match frames to text.
    """

    def _entry(self, *langs):
        return [{
            "id": "vid",
            "text": "whole transcript as one string",
            "languages": [{"label": l, "languageCode": l} for l in langs],
            "tracks": [
                {
                    "language": l,
                    "transcript": [
                        {"start": "0", "dur": "6.951", "text": f"{l} one"},
                        {"start": "6.951", "dur": "2", "text": f"{l} two"},
                    ],
                }
                for l in langs
            ],
        }]

    def test_tracks_give_one_segment_per_caption_line(self):
        segs = ytc._normalise_segments(self._entry("en"))
        self.assertEqual(len(segs), 2)
        self.assertEqual([s["text"] for s in segs], ["en one", "en two"])

    def test_seconds_are_converted_to_milliseconds(self):
        segs = ytc._normalise_segments(self._entry("en"))
        self.assertEqual(segs[0]["offset_ms"], 0)
        self.assertEqual(segs[0]["duration_ms"], 6951)
        self.assertEqual(segs[1]["offset_ms"], 6951)
        self.assertEqual(segs[1]["duration_ms"], 2000)

    def test_the_flat_text_field_is_not_used_as_a_segment(self):
        segs = ytc._normalise_segments(self._entry("en"))
        self.assertNotIn("whole transcript as one string",
                         [s["text"] for s in segs])

    def test_preferred_language_selects_its_track(self):
        segs = ytc._normalise_segments(self._entry("ar", "en"), "en")
        self.assertEqual(segs[0]["text"], "en one")

    def test_region_variant_matches_the_bare_code(self):
        segs = ytc._normalise_segments(self._entry("ar", "en-US"), "en")
        self.assertEqual(segs[0]["text"], "en-US one")

    def test_unavailable_language_falls_back_to_first_track(self):
        # A transcript in the wrong language beats no transcript at all.
        segs = ytc._normalise_segments(self._entry("ar"), "en")
        self.assertEqual(segs[0]["text"], "ar one")

    def test_human_label_matches_via_the_languages_array(self):
        # tracks[].language is a label ("English - English"), not a code;
        # the ISO code only appears in the sibling languages array.
        body = [{
            "id": "vid",
            "languages": [
                {"label": "Japanese", "languageCode": "ja"},
                {"label": "English - English", "languageCode": "en"},
            ],
            "tracks": [
                {"language": "Japanese",
                 "transcript": [{"start": "0", "dur": "1", "text": "ja"}]},
                {"language": "English - English",
                 "transcript": [{"start": "0", "dur": "1", "text": "en"}]},
            ],
        }]
        self.assertEqual(ytc._normalise_segments(body, "en")[0]["text"], "en")
        self.assertEqual(ytc._normalise_segments(body, "ja")[0]["text"], "ja")

    def test_caption_markup_is_unescaped_and_stripped(self):
        body = [{
            "id": "vid",
            "tracks": [{"language": "en", "transcript": [
                {"start": "0", "dur": "1", "text": "&lt;i&gt;Ooh&lt;/i&gt;"},
                {"start": "1", "dur": "1", "text": "rock &amp; roll"},
            ]}],
        }]
        segs = ytc._normalise_segments(body)
        self.assertEqual([s["text"] for s in segs], ["Ooh", "rock & roll"])

    def test_older_shape_still_parses(self):
        body = [{"id": "x", "transcripts": [
            {"text": "legacy", "offset": 0, "duration": 1.0}]}]
        self.assertEqual(ytc._normalise_segments(body)[0]["text"], "legacy")


class NormaliseSegmentsEmptyBodyTest(unittest.TestCase):
    """Empty-body / placeholder-text behavior, pinned for the loud-fail
    contract.

    `_normalise_segments` operates on the parsed JSON. Two separate
    "this transcript is unusable" signals exist in the pipeline:

    - `_normalise_segments([])` -> [] — the response is genuinely empty.
      fetch_transcript then raises ValueError. This is the loud-fail
      path CLAUDE.md documents.

    - `_normalise_segments(<placeholder text>)` -> a list with placeholder
      segments. The placeholder SURVIVES stripping, so it is not the
      empty-trigger. The Thai re-voiced-video case ("[เสียงพากย์ไทย]")
      actually returns a non-empty segments list — fetch_transcript will
      happily write that to disk. Operators diagnose placeholder output
      by inspecting the .txt file, not by the script raising.

    We pin both behaviors here so a future refactor of
    `_normalise_segments` can't silently flip either contract.
    """

    def test_empty_list_yields_no_segments(self):
        # The genuine "no captions available" case. Loud-fail via
        # ValueError in fetch_transcript.
        self.assertEqual(ytc._normalise_segments([]), [])

    def test_placeholder_text_survives_strip_and_is_kept(self):
        # The Thai re-voiced-video case. Placeholder text is non-empty
        # after .strip(), so it is NOT the loud-fail signal. Documenting
        # this so a future "filter placeholders" change is intentional,
        # not accidental.
        body = [{
            "id": "x",
            "transcripts": [{
                "text": "[เสียงพากย์ไทย]",
                "offset": 0,
                "duration": 1.0,
            }],
        }]
        out = ytc._normalise_segments(body)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["text"], "[เสียงพากย์ไทย]")

    def test_real_text_yields_normalised_segment(self):
        # Offset/duration normalisation: the source treats a numeric
        # value < 10000 as seconds (multiplied by 1000), and >= 10000
        # as already-ms. So 1.5 (seconds) -> 1500 ms, not 1.5 ms.
        body = [{
            "id": "x",
            "transcripts": [
                {"text": "  hello world  ", "offset": 1.5, "duration": 2.0},
            ],
        }]
        out = ytc._normalise_segments(body)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["text"], "hello world")  # stripped
        self.assertEqual(out[0]["offset_ms"], 1500)
        self.assertEqual(out[0]["duration_ms"], 2000)

    def test_large_numeric_value_treated_as_already_ms(self):
        # When offset/duration are already large (>= 10000), the source
        # treats them as ms and does not multiply. Documented behavior;
        # pin it. duration=0.5 -> 500ms in the seconds branch.
        body = [{
            "id": "x",
            "transcripts": [
                {"text": "later", "offset": 12345, "duration": 0.5},
            ],
        }]
        out = ytc._normalise_segments(body)
        self.assertEqual(out[0]["offset_ms"], 12345)
        self.assertEqual(out[0]["duration_ms"], 500)


if __name__ == "__main__":
    unittest.main(verbosity=2)
