#!/usr/bin/env python3
"""
Kaltura entry support: turn a pasted `<iframe>` (or its src URL) into a
downloadable MP4 and, when the entry has them, a free caption track.

Why this is not yt-dlp
----------------------
yt-dlp ships a Kaltura extractor and it does not work on the entries this
project cares about: it sends no `Referer`, and a university tenant's
access-control profile answers a referer-less playManifest request with a bare
404. Verified 2026-09-09 against partner 2910381 / entry 1_y9jay9sw
("No video formats found!"), while the same entry downloads fine through the
API calls below. So this module talks to Kaltura's api_v3 directly.

The Referer is the whole trick
------------------------------
Every media URL here is fetched with a `Referer` header. Measured on that
entry: no referer -> 404, `https://example.com/` -> 404, the LMS's own domain
-> 302 to the CDN, and the *Kaltura CDN's own domain* -> 302 as well. The CDN
domain is therefore the default, because it needs no per-institution
configuration; `KALTURA_REFERER` overrides it for a tenant whose access-control
whitelists only their LMS.

The API dance
-------------
1. `session.startWidgetSession` on the partner id gives an anonymous KS. This
   is what the embedded player itself does, so it works for any entry that is
   playable without a login.
2. `baseEntry.getPlaybackContext` returns the playable sources. We take the
   progressive `format=url` MP4 and append the KS — HLS would work too, but a
   single MP4 is what the frames and AssemblyAI stages both want.
3. `captionAsset.list` says whether free captions exist. When one matches the
   requested language we serve it and skip AssemblyAI entirely, exactly like
   the YouTube path skips it.

An entry that needs a real LMS login fails here with a message naming the
partner and entry id. Recording it through the browser is deliberately not
implemented — see CLAUDE.md.

Public API
----------
- looks_like_kaltura(value) -> bool
- parse_input(value) -> KalturaRef
- KalturaRef.metadata() / .media_url() / .captions() / .download(path)
- CLI: `kaltura.py info|url|download|captions <input> [...]`
"""
import html
import json
import os
import re
import sys
from pathlib import Path
from urllib.parse import urlsplit

# NOT a module-level `import requests`. pipeline.sh runs `kaltura.py parse` on
# every input it classifies, and that path must work on any interpreter that
# can reach this file — an ImportError there would silently reclassify a
# perfectly good Kaltura embed as "unrecognized input".
requests = None


def _requests():
    """The requests module, imported on demand.

    A missing dependency is reported as a KalturaError like any other failure:
    it is raised from inside `except` clauses below, where an ImportError would
    surface as a traceback chained onto whatever we were already handling.
    """
    global requests
    if requests is None:
        try:
            import requests as _module
        except ImportError as exc:  # pragma: no cover - environment-dependent
            raise KalturaError(
                "the 'requests' package is not installed in this interpreter, "
                "so the Kaltura API cannot be reached. Run ./setup.sh, or use "
                "the venv python ($MEETING_BOT_VENV/bin/python3)."
            ) from exc
        requests = _module
    return requests


# summarize/retry.py, imported lazily for the same reason as requests: the
# offline `parse` path must not depend on it. Reused rather than reimplemented
# because the project's retry policy is a settled, documented thing —
# exponential backoff with *full* jitter, Retry-After when it is short enough,
# and a status/wording classifier — and a second copy here would drift from it.
# It is pure stdlib, so the lib -> summarize direction costs nothing at import
# time; if that layering is ever tidied up, retry.py is the file to move down
# into lib/.
retry = None


def _retry():
    global retry
    if retry is None:
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "summarize"))
        try:
            import retry as _module
        except ImportError as exc:  # pragma: no cover - a broken checkout
            raise KalturaError(
                "summarize/retry.py could not be imported, so Kaltura requests "
                "cannot be retried. Is the checkout complete?"
            ) from exc
        retry = _module
    return retry


def _with_retries(func, label):
    """Run one HTTP attempt under the project's retry policy."""
    return _retry().with_retries(func, label=label)

# Kaltura's public SaaS CDN. Self-hosted tenants serve api_v3 from their own
# host, which is why the base is taken from the embed URL when there is one and
# this is only the fallback.
DEFAULT_SERVICE_BASE = "https://cdnapisec.kaltura.com"

# See the module docstring: without this header a restricted entry 404s and
# nothing explains why.
DEFAULT_REFERER = "https://cdnapisec.kaltura.com/"

HTTP_TIMEOUT_SECONDS = 60
# Statuses worth another attempt. Kaltura's CDN answers a busy moment with a
# 5xx or simply drops the read — seen live on the deployment box 2026-09-09,
# where a 60s read timeout on getPlaybackContext failed a whole run seconds
# after the same host had answered baseEntry.get fine. They are raised as
# HTTPError so retry.is_retryable sees the status rather than a KalturaError
# it would have to classify by wording.
TRANSIENT_STATUS = {408, 425, 429, 500, 502, 503, 504}
# Media, not JSON: a 90-minute lecture is ~470MB and the connection can stall.
DOWNLOAD_TIMEOUT_SECONDS = 600
DOWNLOAD_CHUNK_BYTES = 1 << 20

# `src="..."` / `src='...'` of an <iframe>. Kaltura's own embed code uses single
# quotes for the src and double quotes for every other attribute, so both have
# to be accepted.
IFRAME_SRC_RE = re.compile(
    r"<iframe[^>]*?\ssrc\s*=\s*(?:\"([^\"]*)\"|'([^']*)')", re.IGNORECASE | re.DOTALL)

# Entry ids are "<partner-ish digit>_<8 alphanumerics>", e.g. 1_y9jay9sw. They
# appear as a query parameter (`entry_id=`) in embed URLs and as a path segment
# (`/entryId/`) in API and playManifest URLs.
ENTRY_ID_RE = re.compile(r"entry_?id[=/]([0-9]+_[A-Za-z0-9]+)", re.IGNORECASE)
# `/p/<id>/` and `/partner_id/<id>/` in paths, `partnerId=`/`partner_id=` in queries.
PARTNER_PATH_RE = re.compile(r"/p(?:artner_id)?/(\d+)", re.IGNORECASE)
PARTNER_QUERY_RE = re.compile(r"partner_?id=(\d+)", re.IGNORECASE)

# Caption asset `format` values (KalturaCaptionType). We can parse the first
# two; anything else (DFXP/TTML XML, SCC) falls back to AssemblyAI rather than
# being half-parsed into a bad transcript.
CAPTION_FORMAT_SRT = "1"
CAPTION_FORMAT_WEBVTT = "3"

# Exit code for "this entry has no usable captions" — transcribe.sh reads it to
# decide whether to fall through to AssemblyAI, and it must not collide with
# the generic failure code 1.
NO_CAPTIONS_EXIT = 3


class KalturaError(RuntimeError):
    """Anything that stops us getting media out of Kaltura."""


def referer():
    """The Referer sent with every request. See the module docstring."""
    return os.environ.get("KALTURA_REFERER") or DEFAULT_REFERER


def looks_like_kaltura(value):
    """Cheap syntactic check — no network. Used to classify pipeline inputs."""
    try:
        parse_input(value)
    except (KalturaError, ValueError):
        return False
    return True


def _iframe_src(value):
    """The src of a pasted <iframe>, HTML-unescaped, or None."""
    match = IFRAME_SRC_RE.search(value)
    if not match:
        return None
    return html.unescape(match.group(1) if match.group(1) is not None else match.group(2))


def parse_input(value):
    """KalturaRef for a pasted <iframe> tag or a Kaltura URL.

    Raises KalturaError when the value is not a Kaltura embed at all, or is one
    we can't pull both ids out of.
    """
    if not value or not str(value).strip():
        raise KalturaError("empty input")
    text = str(value).strip()

    src = _iframe_src(text)
    if src is not None:
        text = src
    # A URL copied out of rendered HTML keeps its entities (`&amp;entry_id=`),
    # which would otherwise leave the entry id unfindable.
    text = html.unescape(text)

    if "kaltura" not in text.lower() and not ENTRY_ID_RE.search(text):
        raise KalturaError("not a Kaltura embed or URL")

    entry_match = ENTRY_ID_RE.search(text)
    if not entry_match:
        raise KalturaError("no entry id found (expected entry_id=1_xxxxxxxx)")
    entry_id = entry_match.group(1)

    partner_match = PARTNER_PATH_RE.search(text) or PARTNER_QUERY_RE.search(text)
    if not partner_match:
        raise KalturaError(f"no partner id found for entry {entry_id} "
                           "(expected /p/<number>/ in the URL)")
    partner_id = partner_match.group(1)

    # Self-hosted tenants serve api_v3 from the same host as the embed.
    base = DEFAULT_SERVICE_BASE
    parts = urlsplit(text)
    if parts.scheme in ("http", "https") and parts.netloc:
        base = f"{parts.scheme}://{parts.netloc}"

    return KalturaRef(partner_id=partner_id, entry_id=entry_id, service_base=base)


def _parse_timecode(value):
    """"HH:MM:SS,mmm" or "HH:MM:SS.mmm" -> milliseconds."""
    value = value.strip().replace(",", ".")
    hours, minutes, seconds = 0, 0, 0.0
    bits = value.split(":")
    if len(bits) == 3:
        hours, minutes, seconds = int(bits[0]), int(bits[1]), float(bits[2])
    elif len(bits) == 2:
        minutes, seconds = int(bits[0]), float(bits[1])
    else:
        seconds = float(bits[0])
    return int(round((hours * 3600 + minutes * 60 + seconds) * 1000))


CUE_RE = re.compile(
    r"(\d{1,2}:\d{2}:\d{2}[.,]\d{1,3}|\d{1,2}:\d{2}[.,]\d{1,3})\s*-->\s*"
    r"(\d{1,2}:\d{2}:\d{2}[.,]\d{1,3}|\d{1,2}:\d{2}[.,]\d{1,3})")
# WebVTT/SRT inline markup, plus the HTML entities Kaltura's captions carry.
CAPTION_TAG_RE = re.compile(r"<[^>]+>")


def parse_caption_cues(text):
    """SRT or WebVTT -> the shared segment shape used by transcribe.sh.

    One parser for both because they differ only in the timecode separator and
    a header line, and the caller has no reason to care which it got.
    """
    segments = []
    blocks = re.split(r"\n\s*\n", text.replace("\r\n", "\n").replace("\r", "\n"))
    for block in blocks:
        lines = [ln for ln in block.split("\n") if ln.strip()]
        if not lines:
            continue
        cue = None
        body = []
        for line in lines:
            match = CUE_RE.search(line)
            if match and cue is None:
                cue = (_parse_timecode(match.group(1)), _parse_timecode(match.group(2)))
                continue
            if cue is not None:
                body.append(line)
        if cue is None:
            continue
        content = CAPTION_TAG_RE.sub("", html.unescape(" ".join(body))).strip()
        if not content:
            continue
        start, end = cue
        segments.append({
            "text": content,
            "offset_ms": start,
            # A zero/negative duration would make an unplayable .srt cue.
            "duration_ms": max(end - start, 1),
        })
    return segments


class KalturaRef:
    """One Kaltura entry, plus the anonymous session used to reach it."""

    def __init__(self, partner_id, entry_id, service_base=DEFAULT_SERVICE_BASE,
                 session=None):
        self.partner_id = str(partner_id)
        self.entry_id = entry_id
        self.service_base = service_base.rstrip("/")
        # Built on first use, not here: pipeline.sh runs `parse` on every
        # input it classifies, and that path must touch nothing but the string.
        self._session = session
        self._ks = None
        self._metadata = None

    @property
    def _http(self):
        if self._session is None:
            self._session = _requests().Session()
        return self._session

    def __repr__(self):
        return f"KalturaRef(partner_id={self.partner_id!r}, entry_id={self.entry_id!r})"

    @property
    def canonical_url(self):
        """A stable link to cite in the summary document."""
        return (f"{self.service_base}/p/{self.partner_id}/embedPlaykitJs/uiconf_id/0"
                f"?iframeembed=true&entry_id={self.entry_id}")

    @property
    def safe_name(self):
        """Filesystem-safe run-name stem, derived from the entry id."""
        return "kal_" + re.sub(r"[^A-Za-z0-9_-]", "", self.entry_id)

    # --- HTTP ---------------------------------------------------------------

    def _headers(self):
        return {"Referer": referer()}

    def _api(self, service, action, params):
        url = f"{self.service_base}/api_v3/service/{service}/action/{action}"
        payload = dict(params)
        payload["format"] = "1"  # JSON
        errors = _requests().exceptions

        def attempt():
            response = self._http.post(url, data=payload, headers=self._headers(),
                                       timeout=HTTP_TIMEOUT_SECONDS)
            if response.status_code in TRANSIENT_STATUS:
                raise errors.HTTPError(
                    f"{service}.{action} returned HTTP {response.status_code}",
                    response=response)
            return response

        try:
            response = _with_retries(attempt, f"kaltura {service}.{action}")
        except errors.RequestException as exc:
            raise KalturaError(f"{service}.{action} request failed: {exc}") from exc
        if response.status_code != 200:
            raise KalturaError(
                f"{service}.{action} returned HTTP {response.status_code}")
        try:
            body = response.json()
        except ValueError as exc:
            raise KalturaError(f"{service}.{action} returned non-JSON") from exc
        # Kaltura reports errors in a 200 body, so the status code proves
        # nothing on its own.
        if isinstance(body, dict) and body.get("objectType") == "KalturaAPIException":
            raise KalturaError(
                f"{service}.{action}: {body.get('message') or body.get('code')}")
        return body

    def ks(self):
        """An anonymous widget session for this partner (cached)."""
        if self._ks is None:
            body = self._api("session", "startWidgetSession",
                             {"widgetId": f"_{self.partner_id}"})
            ks = body.get("ks") if isinstance(body, dict) else None
            if not ks:
                raise KalturaError(
                    f"could not start a widget session for partner {self.partner_id}")
            self._ks = ks
        return self._ks

    # --- Entry facts --------------------------------------------------------

    def metadata(self):
        """The entry's own record: name, description, duration, dimensions."""
        if self._metadata is None:
            body = self._api("baseentry", "get",
                             {"ks": self.ks(), "entryId": self.entry_id})
            if not isinstance(body, dict) or not body.get("id"):
                raise KalturaError(f"entry {self.entry_id} not found")
            self._metadata = body
        return self._metadata

    def title(self):
        return (self.metadata().get("name") or "").strip() or None

    def duration_seconds(self):
        return self.metadata().get("duration")

    def media_url(self):
        """A direct progressive MP4 URL for the best flavor, with the KS attached.

        Prefers `format=url` (one plain MP4 file) over HLS/DASH: the frames
        stage and AssemblyAI both want a file, not a manifest.
        """
        body = self._api("baseentry", "getPlaybackContext", {
            "ks": self.ks(),
            "entryId": self.entry_id,
            "contextDataParams[objectType]": "KalturaContextDataParams",
            "contextDataParams[flavorTags]": "all",
        })
        sources = (body or {}).get("sources") or []
        if not sources:
            messages = [m.get("message") for m in (body or {}).get("messages") or []]
            detail = f" ({'; '.join(m for m in messages if m)})" if messages else ""
            raise KalturaError(
                f"entry {self.entry_id} (partner {self.partner_id}) exposes no "
                f"playable source{detail}. It most likely requires a logged-in "
                "LMS session; this pipeline only handles entries that play "
                "without one.")

        def rank(source):
            fmt = (source.get("format") or "").lower()
            https = "https" in (source.get("protocols") or "")
            return (0 if fmt == "url" else 1 if fmt == "applehttp" else 2,
                    0 if https else 1)

        best = sorted(sources, key=rank)[0]
        url = best.get("url")
        if not url:
            raise KalturaError(f"entry {self.entry_id}: playback source has no URL")
        # The KS is what satisfies access-control on playManifest; without it
        # the CDN answers 404 exactly as it does without a Referer.
        joiner = "&" if "?" in url else "?"
        return f"{url}{joiner}ks={self.ks()}"

    # --- Captions -----------------------------------------------------------

    def caption_assets(self):
        body = self._api("caption_captionasset", "list", {
            "ks": self.ks(),
            "filter[entryIdEqual]": self.entry_id,
        })
        return (body or {}).get("objects") or []

    def captions(self, prefer_language=None):
        """Caption segments for the requested language, or [] if there are none.

        Free and instant when they exist, which is why this is tried before
        paying AssemblyAI — same rationale as the YouTube path. Returns [] (not
        an error) when the entry has no usable track, so the caller can fall
        through.
        """
        assets = [a for a in self.caption_assets()
                  if str(a.get("format")) in (CAPTION_FORMAT_SRT, CAPTION_FORMAT_WEBVTT)]
        if not assets:
            return []

        def score(asset):
            code = (asset.get("languageCode") or "").lower()
            label = (asset.get("language") or "").lower()
            wanted = (prefer_language or "").lower()
            # An exact language-code match first, then the tenant's default
            # track, then anything at all.
            if wanted and (code == wanted or label.startswith(wanted)):
                return 0
            if asset.get("isDefault"):
                return 1
            return 2

        for asset in sorted(assets, key=score):
            text = self._serve_caption(asset["id"])
            segments = parse_caption_cues(text)
            if segments:
                return segments
        return []

    def _serve_caption(self, asset_id):
        url = (f"{self.service_base}/api_v3/service/caption_captionasset/"
               f"action/serve")
        errors = _requests().exceptions

        def attempt():
            response = self._http.get(
                url, params={"captionAssetId": asset_id, "ks": self.ks()},
                headers=self._headers(), timeout=HTTP_TIMEOUT_SECONDS)
            if response.status_code in TRANSIENT_STATUS:
                raise errors.HTTPError(f"caption {asset_id} returned HTTP "
                                       f"{response.status_code}", response=response)
            return response

        try:
            response = _with_retries(attempt, "kaltura captionAsset.serve")
        except errors.RequestException as exc:
            raise KalturaError(f"caption download failed: {exc}") from exc
        if response.status_code != 200:
            raise KalturaError(
                f"caption {asset_id} returned HTTP {response.status_code}")
        return response.text

    # --- Media --------------------------------------------------------------

    def download(self, dest, url=None, progress=True):
        """Stream the entry's MP4 to `dest`. Returns the path written.

        Written to `<dest>.part` and renamed, so an interrupted download can
        never be mistaken for a finished one by the resume logic — the same
        rule the rest of the pipeline follows for artifacts.
        """
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        partial = dest.with_name(dest.name + ".part")
        url = url or self.media_url()
        errors = _requests().exceptions

        # A retry restarts the transfer from zero — there is no Range resume
        # here, because the CDN hands out a signed, time-limited redirect and a
        # half-file is worse than a slow one. Re-downloading ~450MB costs about
        # 30s on this box; failing the stage costs the operator a resume.
        def attempt():
            with self._http.get(url, headers=self._headers(), stream=True,
                                allow_redirects=True,
                                timeout=DOWNLOAD_TIMEOUT_SECONDS) as response:
                if response.status_code in TRANSIENT_STATUS:
                    raise errors.HTTPError(
                        f"media download returned HTTP {response.status_code}",
                        response=response)
                if response.status_code != 200:
                    raise KalturaError(
                        f"media download returned HTTP {response.status_code} "
                        f"(a 404 here usually means the Referer '{referer()}' is "
                        "not allowed by this tenant — set KALTURA_REFERER)")
                total = int(response.headers.get("Content-Length") or 0)
                written = 0
                next_report = 50 << 20
                with open(partial, "wb") as handle:
                    for chunk in response.iter_content(DOWNLOAD_CHUNK_BYTES):
                        if not chunk:
                            continue
                        handle.write(chunk)
                        written += len(chunk)
                        if progress and written >= next_report:
                            pct = f" ({written * 100 // total}%)" if total else ""
                            print(f"    {written >> 20} MB{pct}", file=sys.stderr)
                            next_report += 50 << 20
                # A truncated transfer is a retryable failure, not a short file:
                # without this a dropped connection produces a valid-looking
                # MP4 that ffmpeg then fails on two stages later.
                if total and written < total:
                    raise errors.ConnectionError(
                        f"media download ended early: {written} of {total} bytes")
                return written

        try:
            written = _with_retries(attempt, "kaltura media download")
        except errors.RequestException as exc:
            partial.unlink(missing_ok=True)
            raise KalturaError(f"media download failed: {exc}") from exc
        except KalturaError:
            partial.unlink(missing_ok=True)
            raise
        if written == 0:
            partial.unlink(missing_ok=True)
            raise KalturaError("media download produced an empty file")
        partial.replace(dest)
        return dest


# --- CLI ---------------------------------------------------------------------

USAGE = """Usage:
  kaltura.py parse    <iframe-or-url>            partner/entry as JSON
  kaltura.py info     <iframe-or-url>            title, duration, captions (network)
  kaltura.py url      <iframe-or-url>            a direct, KS-signed MP4 URL
  kaltura.py download <iframe-or-url> <dest>     stream that MP4 to dest
  kaltura.py captions <iframe-or-url> [lang]     caption segments as JSON
                                                 (exit 3 = no usable captions)
"""


def main(argv=None):
    argv = list(sys.argv if argv is None else argv)
    if len(argv) < 3:
        print(USAGE, file=sys.stderr)
        return 1
    command, value = argv[1], argv[2]

    try:
        ref = parse_input(value)
    except KalturaError as exc:
        print(f"kaltura: {exc}", file=sys.stderr)
        return 1

    try:
        if command == "parse":
            print(json.dumps({"partner_id": ref.partner_id,
                              "entry_id": ref.entry_id,
                              "service_base": ref.service_base,
                              "safe_name": ref.safe_name,
                              "url": ref.canonical_url}))
            return 0

        if command == "info":
            meta = ref.metadata()
            captions = [{"id": a.get("id"), "language": a.get("language"),
                         "languageCode": a.get("languageCode"),
                         "format": a.get("format")}
                        for a in ref.caption_assets()]
            print(json.dumps({"partner_id": ref.partner_id,
                              "entry_id": ref.entry_id,
                              "title": ref.title(),
                              "duration": meta.get("duration"),
                              "width": meta.get("width"),
                              "height": meta.get("height"),
                              "captions": captions}, ensure_ascii=False))
            return 0

        if command == "url":
            print(ref.media_url())
            return 0

        if command == "download":
            if len(argv) < 4:
                print(USAGE, file=sys.stderr)
                return 1
            path = ref.download(argv[3])
            print(str(path))
            return 0

        if command == "captions":
            language = argv[3] if len(argv) > 3 else None
            segments = ref.captions(language)
            if not segments:
                print(f"kaltura: entry {ref.entry_id} has no usable caption track",
                      file=sys.stderr)
                return NO_CAPTIONS_EXIT
            json.dump(segments, sys.stdout, ensure_ascii=False)
            print("")
            return 0
    except KalturaError as exc:
        print(f"kaltura: {exc}", file=sys.stderr)
        return 1

    print(USAGE, file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
