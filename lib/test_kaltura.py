#!/usr/bin/env python3
"""Unit tests for lib/kaltura.py — no network, no keys.

Everything that talks to Kaltura goes through a fake requests.Session, so the
tests can assert on *what we send* (the Referer above all) as well as on what
we do with the answer.

    python3 lib/test_kaltura.py
"""
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import kaltura  # noqa: E402


# The real embed code this feature was built against, entities and all.
REAL_IFRAME = (
    '<iframe id="kaltura_player" '
    "src='https://cdnapisec.kaltura.com/p/2910381/embedPlaykitJs/uiconf_id/52668182"
    "?iframeembed=true&amp;entry_id=1_y9jay9sw"
    "&amp;config%5Bprovider%5D=%7B%22widgetId%22%3A%221_xa8ik63w%22%7D"
    "&amp;config%5Bplayback%5D=%7B%22startTime%22%3A0%7D' "
    'style="width: 608px;height: 402px;border: 0;" allowfullscreen '
    'webkitallowfullscreen mozAllowFullScreen '
    'allow="autoplay *; fullscreen *; encrypted-media *" '
    'title="2110322 (2025/2) Online Session on 06-Jan-2026"></iframe>'
)
REAL_SRC = ("https://cdnapisec.kaltura.com/p/2910381/embedPlaykitJs/uiconf_id/52668182"
            "?iframeembed=true&entry_id=1_y9jay9sw")


class FakeResponse:
    def __init__(self, payload=None, status_code=200, text=None):
        self.status_code = status_code
        self._payload = payload
        self.text = text if text is not None else json.dumps(payload)
        self.headers = {}

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


class FakeSession:
    """Records every request and answers from a scripted route table."""

    def __init__(self, routes=None):
        self.routes = routes or {}
        self.calls = []

    def _route(self, url, data=None):
        for key, value in self.routes.items():
            if key in url:
                return value(data) if callable(value) else value
        raise AssertionError(f"unexpected request to {url}")

    def post(self, url, data=None, headers=None, timeout=None):
        self.calls.append({"method": "POST", "url": url, "data": data,
                           "headers": headers or {}})
        return self._route(url, data)

    def get(self, url, params=None, headers=None, timeout=None, **kwargs):
        self.calls.append({"method": "GET", "url": url, "params": params,
                           "headers": headers or {}})
        return self._route(url, params)


WIDGET_SESSION = FakeResponse({"ks": "KS-TOKEN",
                               "objectType": "KalturaStartWidgetSessionResponse"})
ENTRY = FakeResponse({
    "id": "1_y9jay9sw",
    "name": "2110322 (2025/2) Online Session on 06-Jan-2026",
    "duration": 5373,
    "width": 1920,
    "height": 1080,
    "objectType": "KalturaMediaEntry",
})


def playback_context(sources):
    return FakeResponse({"sources": sources, "objectType": "KalturaPlaybackContext"})


MP4_SOURCE = {"format": "url", "protocols": "http,https",
              "url": "https://cdn.example/playManifest/a.mp4"}
HLS_SOURCE = {"format": "applehttp", "protocols": "http,https",
              "url": "https://cdn.example/playManifest/a.m3u8"}


def make_ref(routes):
    return kaltura.KalturaRef("2910381", "1_y9jay9sw", session=FakeSession(routes))


# --- Parsing -----------------------------------------------------------------

class TestParsing(unittest.TestCase):
    def test_the_real_iframe_tag(self):
        ref = kaltura.parse_input(REAL_IFRAME)
        self.assertEqual(ref.partner_id, "2910381")
        self.assertEqual(ref.entry_id, "1_y9jay9sw")
        self.assertEqual(ref.service_base, "https://cdnapisec.kaltura.com")

    def test_the_bare_src_url(self):
        ref = kaltura.parse_input(REAL_SRC)
        self.assertEqual(ref.partner_id, "2910381")
        self.assertEqual(ref.entry_id, "1_y9jay9sw")

    def test_iframe_and_src_agree(self):
        """The two accepted input forms must produce the same run."""
        blob = kaltura.parse_input(REAL_IFRAME)
        url = kaltura.parse_input(REAL_SRC)
        self.assertEqual((blob.partner_id, blob.entry_id, blob.safe_name),
                         (url.partner_id, url.entry_id, url.safe_name))

    def test_html_entities_in_the_src_are_unescaped(self):
        """&amp;entry_id= is what a copied iframe actually contains."""
        self.assertEqual(
            kaltura.parse_input(
                "https://cdnapisec.kaltura.com/p/1/x?a=b&amp;entry_id=1_abcd1234").entry_id,
            "1_abcd1234")

    def test_double_quoted_src(self):
        tag = '<iframe src="https://cdnapisec.kaltura.com/p/7/e?entry_id=0_zzzz1111"></iframe>'
        ref = kaltura.parse_input(tag)
        self.assertEqual((ref.partner_id, ref.entry_id), ("7", "0_zzzz1111"))

    def test_api_style_url_with_path_segments(self):
        ref = kaltura.parse_input(
            "https://cdnapisec.kaltura.com/p/2910381/sp/2910381/playManifest/"
            "entryId/1_y9jay9sw/format/url/name/a.mp4")
        self.assertEqual((ref.partner_id, ref.entry_id), ("2910381", "1_y9jay9sw"))

    def test_self_hosted_host_becomes_the_service_base(self):
        """A tenant on its own domain serves api_v3 from that domain too."""
        ref = kaltura.parse_input(
            "https://video.university.edu/p/55/embedPlaykitJs/u/1?entry_id=1_abcd1234")
        self.assertEqual(ref.service_base, "https://video.university.edu")

    def test_safe_name_is_the_entry_id(self):
        self.assertEqual(kaltura.parse_input(REAL_SRC).safe_name, "kal_1_y9jay9sw")

    def test_non_kaltura_inputs_are_rejected(self):
        for value in ("https://www.youtube.com/watch?v=abcdefghijk",
                      "https://meet.google.com/abc-defg-hij",
                      "/tmp/recording.mp4", "Weekly Standup", "", "   "):
            with self.subTest(value=value):
                self.assertFalse(kaltura.looks_like_kaltura(value))

    def test_a_kaltura_url_with_no_entry_id_is_an_error_not_a_crash(self):
        with self.assertRaises(kaltura.KalturaError):
            kaltura.parse_input("https://cdnapisec.kaltura.com/p/2910381/")

    def test_an_entry_id_with_no_partner_id_is_an_error(self):
        with self.assertRaises(kaltura.KalturaError):
            kaltura.parse_input("https://cdnapisec.kaltura.com/x?entry_id=1_abcd1234")

    def test_looks_like_kaltura_accepts_both_forms(self):
        self.assertTrue(kaltura.looks_like_kaltura(REAL_IFRAME))
        self.assertTrue(kaltura.looks_like_kaltura(REAL_SRC))


# --- The Referer, which is the whole trick -----------------------------------

class TestReferer(unittest.TestCase):
    def setUp(self):
        self._saved = kaltura.os.environ.pop("KALTURA_REFERER", None)

    def tearDown(self):
        if self._saved is not None:
            kaltura.os.environ["KALTURA_REFERER"] = self._saved
        else:
            kaltura.os.environ.pop("KALTURA_REFERER", None)

    def test_default_is_the_cdn_domain(self):
        self.assertEqual(kaltura.referer(), kaltura.DEFAULT_REFERER)

    def test_env_overrides_it(self):
        kaltura.os.environ["KALTURA_REFERER"] = "https://lms.example.edu/"
        self.assertEqual(kaltura.referer(), "https://lms.example.edu/")

    def test_every_api_call_sends_a_referer(self):
        """Without it a restricted entry answers 404 and explains nothing."""
        ref = make_ref({"startWidgetSession": WIDGET_SESSION, "baseentry": ENTRY})
        ref.metadata()
        self.assertTrue(ref._http.calls)
        for call in ref._http.calls:
            self.assertEqual(call["headers"].get("Referer"), kaltura.DEFAULT_REFERER)

    def test_the_configured_referer_reaches_the_request(self):
        kaltura.os.environ["KALTURA_REFERER"] = "https://lms.example.edu/"
        ref = make_ref({"startWidgetSession": WIDGET_SESSION, "baseentry": ENTRY})
        ref.metadata()
        for call in ref._http.calls:
            self.assertEqual(call["headers"].get("Referer"), "https://lms.example.edu/")


# --- The API dance -----------------------------------------------------------

class TestApi(unittest.TestCase):
    def test_widget_session_is_started_for_the_partner(self):
        ref = make_ref({"startWidgetSession": WIDGET_SESSION})
        self.assertEqual(ref.ks(), "KS-TOKEN")
        self.assertEqual(ref._http.calls[0]["data"]["widgetId"], "_2910381")

    def test_the_session_is_cached(self):
        ref = make_ref({"startWidgetSession": WIDGET_SESSION, "baseentry": ENTRY})
        ref.metadata()
        ref.metadata()
        sessions = [c for c in ref._http.calls if "startWidgetSession" in c["url"]]
        self.assertEqual(len(sessions), 1)

    def test_an_api_exception_in_a_200_body_is_an_error(self):
        """Kaltura reports failures in the body, so the status proves nothing."""
        ref = make_ref({"startWidgetSession": FakeResponse(
            {"objectType": "KalturaAPIException", "code": "INVALID_KS",
             "message": "Invalid KS"})})
        with self.assertRaises(kaltura.KalturaError) as caught:
            ref.ks()
        self.assertIn("Invalid KS", str(caught.exception))

    def test_title_and_duration_come_from_the_entry(self):
        ref = make_ref({"startWidgetSession": WIDGET_SESSION, "baseentry": ENTRY})
        self.assertEqual(ref.title(),
                         "2110322 (2025/2) Online Session on 06-Jan-2026")
        self.assertEqual(ref.duration_seconds(), 5373)

    def test_media_url_prefers_the_progressive_mp4_over_hls(self):
        """Both stages downstream want a file, not a manifest."""
        ref = make_ref({"startWidgetSession": WIDGET_SESSION,
                        "getPlaybackContext": playback_context([HLS_SOURCE, MP4_SOURCE])})
        self.assertIn("a.mp4", ref.media_url())

    def test_media_url_carries_the_ks(self):
        """Without it the CDN 404s exactly as it does without a Referer."""
        ref = make_ref({"startWidgetSession": WIDGET_SESSION,
                        "getPlaybackContext": playback_context([MP4_SOURCE])})
        self.assertTrue(ref.media_url().endswith("?ks=KS-TOKEN"))

    def test_media_url_appends_the_ks_to_an_existing_query(self):
        source = dict(MP4_SOURCE, url="https://cdn.example/a.mp4?foo=bar")
        ref = make_ref({"startWidgetSession": WIDGET_SESSION,
                        "getPlaybackContext": playback_context([source])})
        self.assertEqual(ref.media_url(), "https://cdn.example/a.mp4?foo=bar&ks=KS-TOKEN")

    def test_no_sources_says_the_entry_needs_a_login(self):
        """The one failure an operator will actually hit; it must name the ids."""
        ref = make_ref({"startWidgetSession": WIDGET_SESSION,
                        "getPlaybackContext": playback_context([])})
        with self.assertRaises(kaltura.KalturaError) as caught:
            ref.media_url()
        message = str(caught.exception)
        self.assertIn("1_y9jay9sw", message)
        self.assertIn("2910381", message)
        self.assertIn("logged-in", message)


# --- Captions ----------------------------------------------------------------

SRT = """1
00:00:01,000 --> 00:00:03,500
Hello &amp; welcome

2
00:00:03,500 --> 00:00:06,000
<i>Second</i> line
"""

WEBVTT = """WEBVTT

00:00:01.000 --> 00:00:03.500
Hello there

00:00:03.500 --> 00:00:06.000
Second line
"""


class TestCaptionParsing(unittest.TestCase):
    def test_srt(self):
        segments = kaltura.parse_caption_cues(SRT)
        self.assertEqual(len(segments), 2)
        self.assertEqual(segments[0]["offset_ms"], 1000)
        self.assertEqual(segments[0]["duration_ms"], 2500)

    def test_webvtt_dot_separator(self):
        segments = kaltura.parse_caption_cues(WEBVTT)
        self.assertEqual(len(segments), 2)
        self.assertEqual(segments[1]["offset_ms"], 3500)

    def test_entities_are_unescaped_and_markup_dropped(self):
        segments = kaltura.parse_caption_cues(SRT)
        self.assertEqual(segments[0]["text"], "Hello & welcome")
        self.assertEqual(segments[1]["text"], "Second line")

    def test_crlf_input(self):
        self.assertEqual(len(kaltura.parse_caption_cues(SRT.replace("\n", "\r\n"))), 2)

    def test_a_zero_length_cue_still_gets_a_positive_duration(self):
        """A 0ms cue makes an unplayable .srt downstream."""
        segments = kaltura.parse_caption_cues(
            "1\n00:00:05,000 --> 00:00:05,000\nBlip\n")
        self.assertEqual(segments[0]["duration_ms"], 1)

    def test_empty_cues_are_dropped(self):
        segments = kaltura.parse_caption_cues(
            "1\n00:00:01,000 --> 00:00:02,000\n\n\n2\n"
            "00:00:02,000 --> 00:00:03,000\nreal\n")
        self.assertEqual([s["text"] for s in segments], ["real"])

    def test_nothing_parseable_yields_nothing(self):
        self.assertEqual(kaltura.parse_caption_cues("not a caption file"), [])


class TestCaptionSelection(unittest.TestCase):
    def _ref(self, assets, served):
        routes = {
            "startWidgetSession": WIDGET_SESSION,
            "caption_captionasset/action/list": FakeResponse(
                {"objects": assets, "totalCount": len(assets)}),
            "caption_captionasset/action/serve": lambda params: FakeResponse(
                text=served[params["captionAssetId"]], payload=None),
        }
        return kaltura.KalturaRef("2910381", "1_y9jay9sw", session=FakeSession(routes))

    def test_no_caption_assets_returns_empty_not_an_error(self):
        """The caller falls through to AssemblyAI; this is not a failure."""
        ref = self._ref([], {})
        self.assertEqual(ref.captions("th"), [])

    def test_the_requested_language_wins(self):
        ref = self._ref(
            [{"id": "en1", "languageCode": "en", "language": "English", "format": "1"},
             {"id": "th1", "languageCode": "th", "language": "Thai", "format": "1"}],
            {"en1": SRT, "th1": SRT.replace("Hello &amp; welcome", "สวัสดี")})
        self.assertEqual(ref.captions("th")[0]["text"], "สวัสดี")

    def test_the_default_track_is_used_when_the_language_is_absent(self):
        ref = self._ref(
            [{"id": "a", "languageCode": "fr", "language": "French", "format": "1"},
             {"id": "b", "languageCode": "en", "language": "English", "format": "1",
              "isDefault": True}],
            {"a": SRT.replace("Hello &amp; welcome", "bonjour"), "b": SRT})
        self.assertEqual(ref.captions("th")[0]["text"], "Hello & welcome")

    def test_unparseable_formats_are_ignored(self):
        """A DFXP/TTML track half-parsed into a transcript is worse than none."""
        ref = self._ref(
            [{"id": "x", "languageCode": "th", "language": "Thai", "format": "2"}],
            {"x": "<tt><body/></tt>"})
        self.assertEqual(ref.captions("th"), [])

    def test_an_asset_that_serves_nothing_usable_falls_through(self):
        ref = self._ref(
            [{"id": "empty", "languageCode": "th", "language": "Thai", "format": "1"},
             {"id": "good", "languageCode": "en", "language": "English", "format": "1"}],
            {"empty": "", "good": SRT})
        self.assertEqual(len(ref.captions("th")), 2)

    def test_segments_match_the_shape_transcribe_sh_writes(self):
        ref = self._ref(
            [{"id": "a", "languageCode": "th", "language": "Thai", "format": "1"}],
            {"a": SRT})
        for segment in ref.captions("th"):
            self.assertEqual(set(segment), {"text", "offset_ms", "duration_ms"})


# --- Download ----------------------------------------------------------------

class StreamingResponse:
    def __init__(self, chunks, status_code=200):
        self.status_code = status_code
        self._chunks = chunks
        self.headers = {"Content-Length": str(sum(len(c) for c in chunks))}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def iter_content(self, size):
        return iter(self._chunks)


class TestDownload(unittest.TestCase):
    def _ref(self, response):
        routes = {"startWidgetSession": WIDGET_SESSION,
                  "getPlaybackContext": playback_context([MP4_SOURCE]),
                  "playManifest": response}
        return kaltura.KalturaRef("2910381", "1_y9jay9sw", session=FakeSession(routes))

    def test_writes_the_streamed_bytes(self):
        import tempfile
        ref = self._ref(StreamingResponse([b"abc", b"def"]))
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "sub" / "video.mp4"
            ref.download(dest, progress=False)
            self.assertEqual(dest.read_bytes(), b"abcdef")

    def test_no_part_file_is_left_behind(self):
        """A .part must never be mistaken for a finished artifact on resume."""
        import tempfile
        ref = self._ref(StreamingResponse([b"abc"]))
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "video.mp4"
            ref.download(dest, progress=False)
            self.assertFalse((Path(tmp) / "video.mp4.part").exists())

    def test_an_http_error_names_the_referer(self):
        """A 404 here is almost always access-control; say so."""
        import tempfile
        ref = self._ref(StreamingResponse([], status_code=404))
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(kaltura.KalturaError) as caught:
                ref.download(Path(tmp) / "video.mp4", progress=False)
            self.assertIn("KALTURA_REFERER", str(caught.exception))

    def test_an_empty_body_is_a_failure_not_an_empty_file(self):
        import tempfile
        ref = self._ref(StreamingResponse([]))
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "video.mp4"
            with self.assertRaises(kaltura.KalturaError):
                ref.download(dest, progress=False)
            self.assertFalse(dest.exists())
            self.assertFalse(dest.with_suffix(".mp4.part").exists())


# --- The CLI contract the shell scripts depend on ----------------------------

class TestCli(unittest.TestCase):
    def test_parse_prints_the_fields_the_shell_greps_for(self):
        import io
        import contextlib
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = kaltura.main(["kaltura.py", "parse", REAL_IFRAME])
        self.assertEqual(code, 0)
        payload = json.loads(buffer.getvalue())
        # pipeline.sh and run_one.sh sed these two out by name.
        self.assertEqual(payload["safe_name"], "kal_1_y9jay9sw")
        self.assertIn("entry_id=1_y9jay9sw", payload["url"])

    def test_parse_does_not_even_import_requests(self):
        """classify_input runs this for every input, so it must stay offline —
        and must not depend on a library that might not be installed, or a
        good embed would silently classify as "unrecognized input"."""
        saved = kaltura.requests
        kaltura.requests = None
        try:
            self.assertEqual(kaltura.main(["kaltura.py", "parse", REAL_SRC]), 0)
            self.assertIsNone(kaltura.requests)
        finally:
            kaltura.requests = saved

    def test_a_missing_requests_is_a_clean_error_not_a_traceback(self):
        """Seen for real on a box whose venv had no requests: the operator got
        a chained ImportError traceback and a misleading '404?' hint."""
        saved = kaltura.requests
        kaltura.requests = None
        real_import = __builtins__["__import__"] if isinstance(__builtins__, dict) \
            else __builtins__.__import__

        def no_requests(name, *args, **kwargs):
            if name == "requests":
                raise ImportError("No module named 'requests'")
            return real_import(name, *args, **kwargs)

        import builtins
        builtins.__import__ = no_requests
        try:
            with self.assertRaises(kaltura.KalturaError) as caught:
                kaltura._requests()
            self.assertIn("setup.sh", str(caught.exception))
        finally:
            builtins.__import__ = real_import
            kaltura.requests = saved

    def test_a_non_kaltura_input_exits_nonzero(self):
        self.assertEqual(kaltura.main(["kaltura.py", "parse", "/tmp/x.mp4"]), 1)

    def test_no_captions_exits_with_the_documented_code(self):
        """transcribe.sh reads exit 3 as 'fall through to AssemblyAI'."""
        self.assertEqual(kaltura.NO_CAPTIONS_EXIT, 3)


if __name__ == "__main__":
    unittest.main(verbosity=2)
