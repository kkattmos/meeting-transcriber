#!/usr/bin/env python3
"""YouTube's own caption tracks, through yt-dlp — the first choice for a
YouTube transcript since 2026-10-03; youtube-transcript.io is the fallback.

yt-dlp reads the track list itself, so it can tell an uploaded track from
YouTube's automatic captions and both from a machine translation — which
youtube-transcript.io's labels do not (a Thai lecture's automatic captions
came back labelled "en"). YouTube's automatic captions of the spoken audio
are there on almost every video, it is free, and it needs no key.

Which track, in order:
  1. an uploaded track in the requested language (with "auto": in the
     spoken language);
  2. the automatic captions of the language actually SPOKEN — yt-dlp names
     that track "<lang>-orig";
  3. nothing (exit 3).
Never one of YouTube's machine *translations* of the automatic captions
(every other key in `automatic_captions`): this project chooses among the
tracks a video has, it does not translate — a translated ASR track is two
error sources stacked, and the summarizer reads the spoken language fine.

CLI:
    yt_autocaptions.py <youtube_url> [<language>|auto]
prints [{"text", "offset_ms", "duration_ms"}, ...] as JSON on stdout — the
same shape yt_transcript_client.py prints, so transcribe.sh's writer takes
either. Exit 0 on success, 3 when the video has no usable track (the caller
falls back), 1 on any other failure (yt-dlp missing, network, a parse error).

YT_DLP_BIN overrides the yt-dlp executable; it is the test seam.
"""
import glob
import json
import os
import subprocess
import sys
import tempfile

EXIT_NO_TRACK = 3
_TIMEOUT = 180


def _lang_matches(key, want):
    """"en" matches "en", "en-US", "en-GB" — but not "en-orig"'s siblings'
    translations, which never reach this test (see choose_track)."""
    key, want = key.lower(), want.lower()
    return key == want or key.startswith(want + "-") or want.startswith(key + "-")


def choose_track(info, language=None):
    """Return (kind, key) — kind "subtitles" or "automatic_captions" — or None.

    `info` is yt-dlp's -J output. `language` None or "auto" means "whatever
    was spoken".
    """
    want = None if language in (None, "", "auto") else language
    subs = {k: v for k, v in (info.get("subtitles") or {}).items()
            if k != "live_chat" and v}
    auto = info.get("automatic_captions") or {}
    orig = sorted(k for k in auto if k.endswith("-orig") and auto[k])
    # "auto" asks for the spoken language, and an uploaded (human) track in it
    # beats YouTube's speech recognition. The "-orig" key is YouTube's own
    # detection of what was spoken; the video's `language` is what the
    # uploader declared, so it is only the fallback.
    target = want or (orig[0][:-len("-orig")] if orig else info.get("language"))
    if target:
        for key in sorted(subs):
            if _lang_matches(key, target):
                return ("subtitles", key)
    if orig:
        return ("automatic_captions", orig[0])
    # An older yt-dlp without the "-orig" naming: the only automatic track
    # that is certainly not a translation is one in the video's own language.
    spoken = info.get("language")
    if spoken:
        for key in sorted(auto):
            if auto[key] and _lang_matches(key, spoken):
                return ("automatic_captions", key)
    if not want and subs:
        return ("subtitles", sorted(subs)[0])
    return None


def json3_to_segments(data):
    """YouTube's json3 timed text -> [{"text", "offset_ms", "duration_ms"}].

    Events carry their words in `segs`; an event made only of a newline
    (`aAppend` line breaks in automatic captions) carries no text and is
    dropped.
    """
    out = []
    for ev in data.get("events") or []:
        segs = ev.get("segs")
        if not segs:
            continue
        text = " ".join("".join(s.get("utf8", "") for s in segs).split())
        if not text:
            continue
        out.append({
            "text": text,
            "offset_ms": int(ev.get("tStartMs") or 0),
            "duration_ms": int(ev.get("dDurationMs") or 0),
        })
    return out


def _yt_dlp():
    return os.environ.get("YT_DLP_BIN") or "yt-dlp"


def fetch(url, language=None):
    """Return (segments, description). Raises LookupError for "no track"."""
    info_json = subprocess.run(
        [_yt_dlp(), "-J", "--skip-download", "--no-warnings", url],
        capture_output=True, text=True, timeout=_TIMEOUT, check=True).stdout
    info = json.loads(info_json)
    choice = choose_track(info, language)
    if choice is None:
        raise LookupError("the video has no uploaded track in that language "
                          "and no automatic captions of the spoken audio")
    kind, key = choice
    flag = "--write-subs" if kind == "subtitles" else "--write-auto-subs"
    with tempfile.TemporaryDirectory() as tmp:
        subprocess.run(
            [_yt_dlp(), "--skip-download", "--no-warnings", flag,
             "--sub-langs", key, "--sub-format", "json3",
             "-o", os.path.join(tmp, "cap.%(ext)s"), url],
            capture_output=True, text=True, timeout=_TIMEOUT, check=True)
        files = glob.glob(os.path.join(tmp, "*.json3"))
        if not files:
            raise LookupError(f"yt-dlp wrote no json3 file for track {key!r}")
        with open(files[0], encoding="utf-8") as fh:
            segments = json3_to_segments(json.load(fh))
    if not segments:
        raise LookupError(f"track {key!r} is empty")
    label = "uploaded" if kind == "subtitles" else "automatic"
    return segments, f"{label} captions, track {key}"


def main():
    if len(sys.argv) not in (2, 3):
        print(f"Usage: {sys.argv[0]} <youtube_url> [<language>|auto]", file=sys.stderr)
        return 1
    language = sys.argv[2] if len(sys.argv) == 3 else None
    try:
        segments, what = fetch(sys.argv[1], language)
    except LookupError as e:
        print(f"yt-dlp captions: {e}", file=sys.stderr)
        return EXIT_NO_TRACK
    except FileNotFoundError:
        print(f"yt-dlp captions: {_yt_dlp()} is not installed", file=sys.stderr)
        return 1
    except subprocess.CalledProcessError as e:
        tail = (e.stderr or "").strip().splitlines()[-1:] or ["(no output)"]
        print(f"yt-dlp captions: yt-dlp failed: {tail[0]}", file=sys.stderr)
        return 1
    except (subprocess.TimeoutExpired, ValueError) as e:
        print(f"yt-dlp captions: {e}", file=sys.stderr)
        return 1
    print(f"yt-dlp captions: {what}, {len(segments)} segments", file=sys.stderr)
    json.dump(segments, sys.stdout, ensure_ascii=False)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
