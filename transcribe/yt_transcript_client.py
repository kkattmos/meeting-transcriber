#!/usr/bin/env python3
"""
Client for the youtube-transcript.io third-party captions API.

Why this exists: transcribing YouTube audio with whisper.cpp is slow on a
CPU-only VM (several minutes for a typical 20-minute video), and on some
Thai videos Whisper returns only the placeholder "[เสียงพากย์ไทย]" because
the source audio is a re-voiced copy with no recognisable speech. The
youtube-transcript.io service returns YouTube's existing auto-/manual
captions directly, which is orders of magnitude faster and gives back the
real transcript (Thai included).

Multiple accounts / API keys
----------------------------
To spread quota across multiple accounts, this client round-robins through the
numbered keys in the repo-root .env file:

    YT_TRANSCRIPT_KEY_1=acct1-token
    YT_TRANSCRIPT_KEY_2=acct2-token
    ...                             (up to YT_TRANSCRIPT_KEY_10)

The rotation cursor is persisted by lib/keyring.py under
$MEETING_BOT_ROOT/state/keycursor.json, so consecutive runs start on different
accounts instead of always hammering the first one. (Before the Debian 13
port these keys lived in a separate JSON file; they are .env-only now.)

Authentication
--------------
The youtube-transcript.io API uses HTTP Basic auth: the API token is sent
verbatim as the username with an empty password. The request is made via
the `requests` library (urllib was rate-limited on this endpoint while
`curl` worked — most plausibly a User-Agent / TLS-fingerprint difference;
`requests` sends `python-requests/<v>` rather than urllib's
`Python-urllib/<v>`).

Public API
----------
- extract_video_id(url) -> str
- fetch_transcript(video_id) -> list[dict]
    Returns [{'text': str, 'offset_ms': int, 'duration_ms': int}, ...]
- CLI: `python3 -m yt_transcript_client <video_url> [<language>]` prints
  segments to stdout
"""
import html
import json
import os
import re
import sys
from pathlib import Path

import requests
import requests.exceptions

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))
from keyring import KeyRing, missing_keys_message  # noqa: E402

# youtube-transcript.io publishes a 10-account ceiling per operator; the ring
# reads YT_TRANSCRIPT_KEY_1..10 from .env.
KEY_NAME = "YT_TRANSCRIPT_KEY"
MAX_KEYS = 10

# youtube-transcript.io API endpoint. Stable as of 2025; if it changes, only
# this constant needs to move. YT_TRANSCRIPT_API_URL points it somewhere else,
# which is how the end-to-end test exercises this client against a local stub
# server instead of spending a real caption quota on every test run.
API_URL = os.environ.get("YT_TRANSCRIPT_API_URL",
                         "https://www.youtube-transcript.io/api/transcripts")

# Video ID extraction. Matches both youtube.com/watch?v=<id> and youtu.be/<id>.
# 11-char ID matches what YouTube itself enforces; we accept >=6 to be lenient.
VIDEO_ID_RE = re.compile(
    r"(?:youtube\.com/watch\?v=|youtu\.be/)([A-Za-z0-9_-]{6,})"
)

# How long we'll wait for the API before giving up on this key.
HTTP_TIMEOUT_SECONDS = 30

# HTTP statuses that mean "this key is bad / quota exhausted, try the next one".
RETRYABLE_STATUSES = {401, 403, 429, 500, 502, 503, 504}


def extract_video_id(url):
    """Pull the 11-char video ID out of a YouTube URL.

    Raises ValueError if no match. Used by the transcribe.sh wrapper and by
    the CLI entry point below.
    """
    m = VIDEO_ID_RE.search(url)
    if not m:
        raise ValueError(f"Not a recognised YouTube URL: {url!r}")
    return m.group(1)


def _keys_message():
    """The "where do I put my API key?" message.

    Worth spelling out: these tokens used to live in a JSON file under
    /opt/meeting-bot/secrets/, and anyone following an older README will go
    looking for it.
    """
    return missing_keys_message(
        KEY_NAME, MAX_KEYS,
        extra=("\nGet a token from https://www.youtube-transcript.io "
               "(account -> API).\nOnly YouTube inputs need this; local "
               "recordings use ASSEMBLYAI_API_KEY_1..3 instead.\n\n"
               "These tokens no longer live in "
               "/opt/meeting-bot/secrets/youtube_transcript_keys.json — "
               "that file is gone; .env is the only source now."),
    )


def _key_ring():
    """The configured key ring, or SystemExit with instructions if empty."""
    ring = KeyRing.from_env(KEY_NAME, max_slots=MAX_KEYS)
    if not ring:
        raise SystemExit(_keys_message())
    return ring


def _call_api(video_id, api_key):
    """Hit the youtube-transcript.io API once with a single key.

    Returns the parsed JSON response (a list of segment dicts). Raises:
      - RuntimeError on retryable HTTP failure (caller should try the next key)
      - requests.HTTPError on non-retryable HTTP failure (propagated)
      - requests.exceptions.RequestException on network failures (propagated)

    Why `requests` instead of stdlib urllib: `curl` against this endpoint
    succeeds where urllib was being throttled — the most plausible cause
    is a User-Agent / TLS-fingerprint difference. requests sends
    `python-requests/<v>` instead of urllib's `Python-urllib/<v>`. We log
    the outbound User-Agent so a future failure has a clean datapoint.
    """
    # Diagnostic: log the User-Agent we're about to send. Captured by
    # transcribe.sh into yt-client.log via stderr.
    print(
        f"  User-Agent: {requests.utils.default_user_agent()}",
        file=sys.stderr,
    )

    try:
        resp = requests.post(
            API_URL,
            headers={
                "Authorization": f"Basic {api_key}",
                "Content-Type": "application/json",
            },
            json={"ids": [video_id]},
            timeout=HTTP_TIMEOUT_SECONDS,
        )
    except requests.exceptions.RequestException:
        # Network / TLS / DNS / connection failures — propagate so the
        # caller's `except RequestException` branch can advance to the
        # next key.
        raise

    if resp.status_code in RETRYABLE_STATUSES:
        raise RuntimeError(
            f"youtube-transcript.io HTTP {resp.status_code}: {resp.reason} — "
            f"key may be rate-limited or invalid"
        )

    if resp.status_code >= 400:
        # Non-retryable: surface as HTTPError so the round-robin loop
        # can still distinguish it from a transient blip if needed.
        resp.raise_for_status()

    return resp.json()


def _clean_caption_text(text):
    """Unescape entities and drop caption markup.

    YouTube caption cues arrive HTML-escaped, so italics reach us as the
    literal characters "&lt;i&gt;" and an ampersand as "&amp;". Left alone
    they end up in the .txt, in the .srt, and inside the <details> block of
    the summary document.
    """
    text = html.unescape(str(text))
    # Caption markup is only ever simple tags like <i> / </i> / <b>.
    text = re.sub(r"</?[a-zA-Z][^>]*>", "", text)
    return text.strip()


def _pick_track(entry, prefer_language=None):
    """Return the transcript segment list from an entry's `tracks`, or None.

    The current API shape (verified 2026-09) is:

        [{"id": ..., "title": ..., "text": "<whole transcript, one string>",
          "languages": [{"label": "en", "languageCode": "en"}, ...],
          "tracks": [{"language": "en",
                      "transcript": [{"start": "6.951", "dur": "2",
                                      "text": "..."}, ...]}]}]

    `tracks[].transcript` is the only place per-segment timing lives, and
    timing is what lets chunking hand each chunk the frames that were on
    screen while those words were spoken. The flat `text` field has none.
    """
    tracks = entry.get("tracks")
    if not isinstance(tracks, list) or not tracks:
        return None
    chosen = None
    if prefer_language:
        # A track's `language` is a human label ("English - English",
        # "Japanese"), not a code. The sibling `languages` array carries the
        # ISO code for the same label, in the same order, so use it to
        # translate before matching — otherwise every comparison against
        # "en" fails and we silently fall back to tracks[0].
        code_by_label = {}
        for lang in entry.get("languages") or []:
            if isinstance(lang, dict) and lang.get("label"):
                code_by_label[str(lang["label"])] = str(lang.get("languageCode") or "")
        want = str(prefer_language).lower()
        for t in tracks:
            label = str(t.get("language") or "")
            candidates = {label.lower(), code_by_label.get(label, "").lower()}
            candidates.discard("")
            # "en" should match "en-US" and vice versa.
            if any(c == want or c.startswith(want + "-") or want.startswith(c + "-")
                   for c in candidates):
                chosen = t
                break
    if chosen is None:
        chosen = tracks[0]
    segments = chosen.get("transcript")
    return segments if isinstance(segments, list) else None


def _normalise_segments(raw, prefer_language=None):
    """Convert the API's response shape into a flat list of segments.

    The API's exact field names are documented to drift between versions, so
    we accept a few common variants. Returns [] if the response doesn't look
    like a non-empty transcript (so the caller can fail loudly).
    """
    if not isinstance(raw, list) or not raw:
        return []

    out = []
    for entry in raw:
        # Current shape first: the timed segments under tracks[].transcript.
        # Without this the fallbacks below land on the entry dict itself and
        # its flat `text` field, producing ONE segment holding the entire
        # transcript with no timing at all — which is exactly what happened
        # before this branch existed.
        segments = _pick_track(entry, prefer_language)
        if segments is None:
            # Older/other shapes: segments under 'transcripts' or 'segments',
            # or the entry itself.
            segments = entry.get("transcripts") or entry.get("segments") or entry
        if isinstance(segments, dict):
            segments = [segments]
        if not isinstance(segments, list):
            continue

        for seg in segments:
            text = _clean_caption_text(
                seg.get("text")
                or seg.get("transcript")
                or seg.get("caption")
                or ""
            )
            # Offset / duration can come back as either seconds (float) or
            # milliseconds (int). Normalise to int milliseconds.
            offset = seg.get("offset") or seg.get("start") or seg.get("startMs") or 0
            duration = seg.get("duration") or seg.get("dur") or seg.get("durationMs") or 0
            try:
                offset_ms = int(float(offset) * 1000) if float(offset) < 10000 else int(offset)
            except (TypeError, ValueError):
                offset_ms = 0
            try:
                duration_ms = int(float(duration) * 1000) if float(duration) < 10000 else int(duration)
            except (TypeError, ValueError):
                duration_ms = 0
            if text:
                out.append({
                    "text": text,
                    "offset_ms": offset_ms,
                    "duration_ms": duration_ms,
                })
    return out


def fetch_transcript(video_id, prefer_language=None, ring=None):
    """Fetch a transcript for `video_id`, trying each configured key in
    round-robin order until one succeeds.

    `prefer_language` picks among the caption tracks the video actually has
    ("en" matches "en-US"); the first track is used when it doesn't match,
    since a transcript in the wrong language still beats no transcript.

    Returns a list of {'text', 'offset_ms', 'duration_ms'} segments.

    Raises SystemExit (with the last error) if every key fails.
    Raises ValueError if the API returns no usable segments for the video.
    """
    ring = ring or _key_ring()

    last_error = None
    for slot, key in ring.rotate():
        try:
            raw = _call_api(video_id, key)
        except RuntimeError as e:
            print(f"  {ring.label(slot)} failed: {e}", file=sys.stderr)
            last_error = e
            continue
        except requests.exceptions.RequestException as e:
            # `requests` exceptions don't always have a `.reason` attribute
            # (only some subclasses do). Fall back to str(e) for a
            # human-readable label.
            reason = getattr(e, "reason", None) or str(e)
            print(f"  {ring.label(slot)} network error: {reason}",
                  file=sys.stderr)
            last_error = e
            continue

        # Success — advance the persisted cursor so the *next* run starts on a
        # different account.
        ring.commit(slot)

        segments = _normalise_segments(raw, prefer_language)
        if not segments:
            # The API returned 200 but no usable segments. Don't bother
            # retrying on a different key — every key hits the same
            # upstream YouTube captions. Fail loudly per the user's choice.
            raise ValueError(
                f"youtube-transcript.io returned no usable transcript for "
                f"video {video_id!r}. The video may have no captions, or "
                f"all captions are placeholders (e.g. '[เสียงพากย์ไทย]'). "
                f"Not retrying on another key — every account hits the "
                f"same upstream data."
            )
        return segments

    # All keys failed. Surface the most informative error we saw.
    raise SystemExit(
        f"All {len(ring)} youtube-transcript.io key(s) failed for video "
        f"{video_id!r}. Last error: {last_error}"
    )


def _format_srt_timestamp(ms):
    """Format an integer millisecond offset as 'HH:MM:SS,mmm' for SRT."""
    s, ms = divmod(int(ms), 1000)
    m, s = divmod(s, 60)
    h, m = divmod(m, 60)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def write_outputs(segments, txt_path, srt_path):
    """Write the two on-disk artefacts expected by downstream stages.

    .txt: blank-line-separated segments (matches whisper.cpp's -otxt output).
    .srt: standard SubRip format with 1-based indices.
    """
    lines = [seg["text"] for seg in segments]
    txt_path.write_text("\n".join(lines) + "\n")

    srt_chunks = []
    for i, seg in enumerate(segments, start=1):
        start_ms = seg["offset_ms"]
        end_ms = start_ms + max(seg["duration_ms"], 1)
        srt_chunks.append(
            f"{i}\n"
            f"{_format_srt_timestamp(start_ms)} --> {_format_srt_timestamp(end_ms)}\n"
            f"{seg['text']}\n"
        )
    srt_path.write_text("\n".join(srt_chunks))


def main():
    """CLI entry point: `python3 -m yt_transcript_client <youtube_url>`.

    Prints the segments as JSON to stdout (so the bash wrapper can pipe it
    to the writer). Exits non-zero on failure.
    """
    if len(sys.argv) not in (2, 3):
        print(f"Usage: {sys.argv[0]} <youtube_url> [<language>]",
              file=sys.stderr)
        sys.exit(1)
    video_id = extract_video_id(sys.argv[1])
    # "auto" means "whatever the video has" — same as passing nothing.
    prefer = sys.argv[2] if len(sys.argv) == 3 else None
    if prefer in ("auto", ""):
        prefer = None
    segments = fetch_transcript(video_id, prefer_language=prefer)
    json.dump(segments, sys.stdout, ensure_ascii=False)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()