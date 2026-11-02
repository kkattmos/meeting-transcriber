#!/usr/bin/env python3
"""
Client for the AssemblyAI pre-recorded transcription API.

Why this exists: whisper.cpp on the 4-vCPU VM is slow (minutes per
recording) and was the source of multiple Thai-transcription issues. The
AssemblyAI pre-recorded API uploads an MP4/M4A/WAV directly, transcribes
asynchronously, and returns the text + word-level timing in seconds.

Public API
----------
- transcribe_file(path, language) -> list[dict]
    Returns [{'text': str, 'offset_ms': int, 'duration_ms': int}, ...] —
    one segment per *sentence* (falling back to one per word if the
    sentences endpoint is unavailable). Same shape as
    yt_transcript_client.fetch_transcript() so transcribe.sh's writer can
    consume both backends without branching.
- CLI: `python3 -m assemblyai_client <local_audio_or_video_path> [<language>]`
    Prints the segment list as JSON to stdout. Exits non-zero on failure.

Configuration
-------------
- ASSEMBLYAI_API_KEY_1..3  at least one required; read from env (or sourced
                          via .env -> transcribe.sh). Several keys are
                          rotated round-robin by lib/keyring.py to spread
                          quota across accounts, and a key rejected for
                          auth/quota reasons hands over to the next one.
                          The unnumbered ASSEMBLYAI_API_KEY still works and
                          counts as slot 1.
- ASSEMBLYAI_MODEL       optional; default "universal-2". The SDK accepts
                          an ordered fallback list; setting this env var
                          overrides the leading entry. See
                          https://www.assemblyai.com/docs for current model
                          names. Per CLAUDE.md's AssemblyAI agent notes:
                          "speech_models (pre-recorded) is optional —
                          defaults to ['universal-3-5-pro', 'universal-2'];
                          it's an ordered fallback list, not parallel".
- ASSEMBLYAI_POLL_SECONDS  optional; default 5. How long to wait between
                          polls of the transcript status endpoint.
- ASSEMBLYAI_BASE_URL    optional; overrides the API host. Used by the
                          end-to-end test to run the real SDK against a local
                          stub server rather than spending transcription
                          minutes on every test run.

Failure modes
-------------
- Missing API key: SystemExit with the env-var name and a one-line
  resolution hint. Mirrors yt_transcript_client.py's startup behaviour.
- API error (status == error): SystemExit with the upstream message.
- Empty response (zero words): SystemExit exit code 2, matching
  transcribe.sh's existing "empty transcript" loud-fail behaviour. Each
  API key hits the same upstream model so retrying won't help.
"""
import json
import os
import sys
import time
from pathlib import Path

# The SDK auto-uploads via `transcribe(path)`. We import lazily so the
# module is still importable (for unit tests) without the SDK installed —
# the CLI entry point will fail loudly if the import fails.
import assemblyai as aai  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))
from keyring import KeyRing, missing_keys_message  # noqa: E402

KEY_NAME = "ASSEMBLYAI_API_KEY"
MAX_KEYS = 3

DEFAULT_MODEL = "universal-2"
DEFAULT_LANGUAGE = "th"
POLL_SECONDS = float(os.environ.get("ASSEMBLYAI_POLL_SECONDS", "5"))


def _key_ring():
    """The configured AssemblyAI keys, or fail loudly.

    Resolution order (matches source_env.sh semantics): an already-exported
    env var wins over .env. We read directly because this script is invoked
    as a subprocess of transcribe.sh which has already loaded .env.
    """
    ring = KeyRing.from_env(KEY_NAME, max_slots=MAX_KEYS)
    if not ring:
        raise SystemExit(missing_keys_message(
            KEY_NAME, MAX_KEYS,
            extra="\nGet a key at https://www.assemblyai.com.",
        ))
    return ring


# Wording that means "this key can't be used" rather than "this audio can't be
# transcribed". Only the former is worth handing to the next key: rotating
# through three accounts because the file is silent just wastes three uploads.
_KEY_LEVEL_ERROR_MARKERS = (
    "unauthorized", "invalid api key", "authentication", "forbidden",
    "insufficient funds", "exceeded", "quota", "rate limit",
    "too many requests", "401", "403", "429",
)


def _is_key_level_error(exc):
    text = f"{type(exc).__name__}: {exc}".lower()
    return any(marker in text for marker in _KEY_LEVEL_ERROR_MARKERS)


def _build_config(language):
    """Build a TranscriptionConfig with optional language and model override.

    The default speech_models list is the SDK's own fallback chain
    (["universal-3-5-pro", "universal-2"]); we only override the leading
    entry when ASSEMBLYAI_MODEL is set. language_code is set unless the
    caller asked for "auto", in which case we rely on AssemblyAI's
    automatic detection (best-effort, may mis-route on multilingual
    audio).
    """
    kwargs = {}
    if language and language != "auto":
        kwargs["language_code"] = language
    model = os.environ.get("ASSEMBLYAI_MODEL")
    if model:
        # ASSEMBLYAI_MODEL is the preferred entry in the fallback chain;
        # universal-2 is the always-on safety net.
        kwargs["speech_models"] = [model, "universal-2"]
    return aai.TranscriptionConfig(**kwargs)


def _words_to_segments(words):
    """Convert AssemblyAI's Word list into our segment shape.

    One word == one segment. Duration is end - start (ms). Words without
    timing (start/end == None — the SDK returns these when word timestamps
    are disabled) are emitted with offset=0 and duration=0 so the .srt
    writer can still place them in the file.
    """
    segments = []
    for w in words:
        text = (getattr(w, "text", "") or "").strip()
        if not text:
            continue
        start = getattr(w, "start", None)
        end = getattr(w, "end", None)
        if start is None or end is None:
            # Per CLAUDE.md: SDK defaults to start/end=None unless
            # timestamps=True. We don't opt in (cost in latency) — just
            # emit placeholder timing so downstream stages still work.
            segments.append({
                "text": text,
                "offset_ms": 0,
                "duration_ms": 0,
            })
        else:
            segments.append({
                "text": text,
                "offset_ms": int(start),
                "duration_ms": max(int(end) - int(start), 1),
            })
    return segments


def _sentences_to_segments(sentences):
    """Convert AssemblyAI's Sentence list into our segment shape.

    One sentence == one segment. This is the granularity we actually want:
    the .txt becomes readable prose (it is embedded verbatim in the summary
    document) and the .srt becomes real subtitles rather than one word per
    cue. It also matches what the YouTube backend emits, so both backends
    produce comparable output.
    """
    segments = []
    for s in sentences:
        text = (getattr(s, "text", "") or "").strip()
        if not text:
            continue
        start = getattr(s, "start", None)
        end = getattr(s, "end", None)
        if start is None or end is None:
            segments.append({"text": text, "offset_ms": 0, "duration_ms": 0})
        else:
            segments.append({
                "text": text,
                "offset_ms": int(start),
                "duration_ms": max(int(end) - int(start), 1),
            })
    return segments


def _transcribe_once(path, config, lang):
    """One submission with whatever key is currently set on the SDK."""
    print(f"==> Submitting {path.name} to AssemblyAI (language={lang})",
          file=sys.stderr)
    transcriber = aai.Transcriber(config=config)
    transcript = transcriber.transcribe(str(path))

    if transcript.status == aai.TranscriptStatus.error:
        raise RuntimeError(f"AssemblyAI transcription failed: {transcript.error}")

    # Some SDK versions expose transcript.words only after status is
    # 'completed'. Confirm before reading.
    if transcript.status != aai.TranscriptStatus.completed:
        raise RuntimeError(
            f"AssemblyAI transcript is in unexpected status "
            f"{transcript.status!r}; expected 'completed'."
        )
    return transcript


def transcribe_file(path, language=None, ring=None):
    """Submit a local audio/video file to AssemblyAI and return segments.

    Args:
        path: absolute path to a local audio or video file. AssemblyAI's
              pre-recorded API accepts mp3, mp4, m4a, wav, webm, ogg.
        language: ISO-639-1 code (e.g. "th", "en") or "auto". Defaults to
              the env var ASSEMBLYAI_LANGUAGE or "th".
        ring: key ring to rotate through; defaults to ASSEMBLYAI_API_KEY_1..3.

    Returns:
        list[dict] with the {text, offset_ms, duration_ms} shape.

    Raises:
        SystemExit on missing key, upstream error, or empty transcript.
    """
    ring = ring or _key_ring()

    path = Path(path)
    if not path.is_file():
        raise SystemExit(f"Input file not found: {path}")

    lang = language or os.environ.get("ASSEMBLYAI_LANGUAGE", DEFAULT_LANGUAGE)
    config = _build_config(lang)

    base_url = os.environ.get("ASSEMBLYAI_BASE_URL")
    if base_url:
        aai.settings.base_url = base_url

    transcript = None
    last_error = None
    for slot, key in ring.rotate():
        aai.settings.api_key = key
        try:
            transcript = _transcribe_once(path, config, lang)
        except Exception as exc:  # noqa: BLE001 - SDK raises many shapes
            if _is_key_level_error(exc) and len(ring) > 1:
                print(f"  {ring.label(slot)} rejected: {exc}", file=sys.stderr)
                last_error = exc
                continue
            # Not a credential problem — another key would fail identically.
            raise SystemExit(str(exc)) from exc
        ring.commit(slot)
        break

    if transcript is None:
        raise SystemExit(
            f"All {len(ring)} AssemblyAI key(s) were rejected for {path.name}. "
            f"Last error: {last_error}"
        )

    # Sentences first, words only as a fallback. get_sentences() is a
    # second (free) GET against /v2/transcript/<id>/sentences; if it fails
    # or the SDK is too old to have it, word-level output is still usable,
    # just ugly — a transcript we can read beats no transcript at all.
    segments = []
    get_sentences = getattr(transcript, "get_sentences", None)
    if callable(get_sentences):
        try:
            segments = _sentences_to_segments(get_sentences() or [])
        except Exception as exc:  # noqa: BLE001 - any SDK/API failure
            print(
                f"WARNING: could not fetch sentence-level transcript "
                f"({exc.__class__.__name__}: {exc}); falling back to "
                "word-level segments.",
                file=sys.stderr,
            )
    if not segments:
        words = getattr(transcript, "words", None) or []
        segments = _words_to_segments(words)

    if not segments:
        # Mirror transcribe.sh's exit-2 loud-fail for empty responses.
        # Every key hits the same upstream model, so retrying won't help.
        print(
            "ERROR: AssemblyAI returned no usable transcript "
            f"for {path.name}. The audio may be silent, in an unsupported "
            "language, or the upstream model failed. Check the audio file "
            "and try again.",
            file=sys.stderr,
        )
        sys.exit(2)

    return segments


def main():
    """CLI entry point. Prints segments as JSON to stdout."""
    if len(sys.argv) < 2 or len(sys.argv) > 3:
        print(
            f"Usage: {sys.argv[0]} <local_audio_or_video_path> [<language>]",
            file=sys.stderr,
        )
        sys.exit(1)
    path = sys.argv[1]
    language = sys.argv[2] if len(sys.argv) > 2 else None
    segments = transcribe_file(path, language=language)
    json.dump(segments, sys.stdout, ensure_ascii=False)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
