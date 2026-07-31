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
To spread quota across multiple accounts, this client round-robins through
a list of keys stored at /opt/meeting-bot/secrets/youtube_transcript_keys.json:

    {
        "keys":   ["acct1-token", "acct2-token", "acct3-token"],
        "next_index": 0   # cursor — advances after each successful pick
    }

The cursor is persisted back to disk so a sequence of runs deterministically
walks the list. Use chmod 600 on the file (the script will enforce this on
first write).

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
- CLI: `python3 -m yt_transcript_client <video_url>` prints segments to stdout
"""
import json
import os
import re
import sys
from pathlib import Path

import requests
import requests.exceptions

# Canonical path for the keys file. Matches what setup.sh creates and what
# .gitignore excludes. Override with YT_TRANSCRIPT_KEYS_FILE for tests.
# Falls back to the YT_TRANSCRIPT_KEYS_FILE env var so users can point it
# somewhere else via .env without touching code.
DEFAULT_KEYS_FILE = Path(
    os.environ.get(
        "YT_TRANSCRIPT_KEYS_FILE",
        "/opt/meeting-bot/secrets/youtube_transcript_keys.json",
    )
)

# youtube-transcript.io API endpoint. Stable as of 2025; if it changes, only
# this constant needs to move.
API_URL = "https://www.youtube-transcript.io/api/transcripts"

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


def _read_keys_file(path):
    """Load the keys file. Returns the parsed dict; creates a starter file
    with an empty keys list if the file doesn't exist yet (so the user can
    edit it without running a separate init step)."""
    if not path.exists():
        # Bootstrap a starter file. Caller is expected to edit it; we just
        # make the workflow discoverable.
        path.parent.mkdir(parents=True, exist_ok=True)
        starter = {"keys": [], "next_index": 0, "_comment": (
            "Add your youtube-transcript.io API tokens to the 'keys' list. "
            "Each entry is a separate account. Save the file and re-run."
        )}
        path.write_text(json.dumps(starter, indent=2) + "\n")
        try:
            os.chmod(path, 0o600)
        except OSError:
            # Non-fatal — log via stderr and continue. The file is still
            # root-owned in most setups.
            print(
                f"warning: could not chmod 600 {path} — please do it manually",
                file=sys.stderr,
            )
        raise SystemExit(
            f"Created starter keys file at {path}. Add at least one API "
            f"token to its 'keys' list and re-run."
        )

    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError as e:
        raise SystemExit(f"Keys file {path} is not valid JSON: {e}")

    keys = data.get("keys") or []
    if not isinstance(keys, list) or not keys:
        raise SystemExit(
            f"Keys file {path} has an empty 'keys' list. Add at least one "
            f"API token."
        )
    cursor = int(data.get("next_index", 0)) % len(keys)
    return {"keys": keys, "next_index": cursor, "_path": path}


def _write_cursor(path, cursor):
    """Persist the round-robin cursor back to disk.

    Best-effort: we don't want a transient write failure (disk full, perms)
    to mask a successful transcript fetch. The next run will pick the same
    key again, which is fine — the worst case is one duplicate request.
    """
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        # If we can't read it, we can't update it; bail silently.
        return
    data["next_index"] = cursor % max(1, len(data.get("keys") or []))
    try:
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, indent=2) + "\n")
        tmp.replace(path)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    except OSError as e:
        print(
            f"warning: could not persist cursor to {path}: {e}",
            file=sys.stderr,
        )


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


def _normalise_segments(raw):
    """Convert the API's response shape into a flat list of segments.

    The API's exact field names are documented to drift between versions, so
    we accept a few common variants. Returns [] if the response doesn't look
    like a non-empty transcript (so the caller can fail loudly).
    """
    if not isinstance(raw, list) or not raw:
        return []

    out = []
    for entry in raw:
        # Most API versions return one entry per video, with segments under
        # 'transcripts' or 'segments'. Some return segments directly.
        segments = entry.get("transcripts") or entry.get("segments") or entry
        if isinstance(segments, dict):
            segments = [segments]
        if not isinstance(segments, list):
            continue

        for seg in segments:
            text = (
                seg.get("text")
                or seg.get("transcript")
                or seg.get("caption")
                or ""
            ).strip()
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


def fetch_transcript(video_id, keys_file=None):
    """Fetch a transcript for `video_id`, trying each configured key in
    round-robin order until one succeeds.

    Returns a list of {'text', 'offset_ms', 'duration_ms'} segments.

    Raises SystemExit (with the last error) if every key fails.
    Raises ValueError if the API returns no usable segments for the video.
    """
    path = Path(keys_file) if keys_file else DEFAULT_KEYS_FILE
    state = _read_keys_file(path)
    keys = state["keys"]
    cursor = state["next_index"]
    keys_path = state["_path"]

    last_error = None
    for i, key in enumerate(keys):
        pick = (cursor + i) % len(keys)
        try:
            raw = _call_api(video_id, keys[pick])
        except RuntimeError as e:
            print(
                f"  key #{pick + 1}/{len(keys)} failed: {e}",
                file=sys.stderr,
            )
            last_error = e
            continue
        except requests.exceptions.RequestException as e:
            # `requests` exceptions don't always have a `.reason` attribute
            # (only some subclasses do). Fall back to str(e) for a
            # human-readable label.
            reason = getattr(e, "reason", None) or str(e)
            print(
                f"  key #{pick + 1}/{len(keys)} network error: {reason}",
                file=sys.stderr,
            )
            last_error = e
            continue

        # Success — persist the cursor for the next call. Advance past the
        # key we just used so the *next* request lands on a different one.
        next_cursor = (pick + 1) % len(keys)
        _write_cursor(keys_path, next_cursor)

        segments = _normalise_segments(raw)
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
        f"All {len(keys)} youtube-transcript.io key(s) failed for video "
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
    if len(sys.argv) != 2:
        print(f"Usage: {sys.argv[0]} <youtube_url>", file=sys.stderr)
        sys.exit(1)
    video_id = extract_video_id(sys.argv[1])
    segments = fetch_transcript(video_id)
    json.dump(segments, sys.stdout, ensure_ascii=False)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()