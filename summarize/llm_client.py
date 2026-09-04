#!/usr/bin/env python3
"""
Pluggable LLM client for the meeting-summary agent.

Two backends plus an auto-fallback chain, all selected by env var:

  Auto-fallback (`SUMMARY_BACKEND=fallback`, the default) - walks
  `SUMMARY_FALLBACK_CHAIN` in order. Each entry is one of: `anthropic`
  (aliases: `claude`, `fcc`) or `gemini`. A backend is only abandoned after
  its own retries are exhausted; the first to return wins. If every backend
  fails, the accumulated history is raised. Default chain:
  `anthropic,gemini`.

  Anthropic Claude (`SUMMARY_BACKEND=anthropic`) - the default and primary
  backend. Reads ANTHROPIC_MODEL (default claude-opus-5), SUMMARY_EFFORT, and
  a single ANTHROPIC_API_KEY. Works against api.anthropic.com or any proxy
  that speaks the Messages API, because the SDK honors ANTHROPIC_BASE_URL.

  Google Gemini (`SUMMARY_BACKEND=gemini`) - the google-genai SDK, with up to
  three keys rotated round-robin (GEMINI_API_KEY_1..3).

EFFORT, NOT A TOKEN BUDGET. SUMMARY_EFFORT maps straight onto the Messages
API's `output_config.effort` (low | medium | high | xhigh | max), which is how
current Claude models are told how hard to think. The older
`thinking.budget_tokens` knob is rejected outright by Opus 5, so there is
deliberately no token-budget setting here. Thinking itself is adaptive: the
model decides when to use it.

SDK-VERSION TOLERANCE. `output_config` and `thinking` are passed as normal
keyword arguments and, if the installed SDK is too old to know them, retried
inside `extra_body`. That keeps a stale `pip install anthropic` from turning
into a hard failure of the whole summarize stage on a box nobody has updated.

TRANSIENT FAILURES. Every backend's network call goes through
summarize/retry.py: 503 "server is busy", 429, 5xx and connection errors are
retried with exponential backoff and full jitter (honoring Retry-After), and
only a backend that keeps failing hands over to the next in the chain. See
retry.py for the policy and its env vars.

MISSING CREDENTIALS raise BackendUnavailable, which the chain treats as "skip
this one" rather than a fatal error - a chain of two backends shouldn't die
because the first one's key isn't configured.

Both backends take the same extracted frames; the prompt and frame-list shape
don't vary by provider.

Env vars:
  SUMMARY_BACKEND       "fallback" (default), "anthropic", or "gemini"
  SUMMARY_FALLBACK_CHAIN  default "anthropic,gemini"
  ANTHROPIC_API_KEY     required for anthropic (ANTHROPIC_API_KEY_1 also read)
  ANTHROPIC_MODEL       default claude-opus-5 (SUMMARY_MODEL also accepted)
  ANTHROPIC_BASE_URL    default https://api.anthropic.com
  SUMMARY_EFFORT        low | medium | high (default) | xhigh | max
  GEMINI_API_KEY_1..3   required for gemini (GOOGLE_API_KEY also accepted)
  GEMINI_MODEL          default gemini-3.6-flash
  SUMMARY_MAX_TOKENS    default 16000
"""
import base64
import mimetypes
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import List

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))

from retry import with_retries  # noqa: E402
from keyring import KeyRing, missing_keys_message  # noqa: E402

DEFAULT_BACKEND = "fallback"
DEFAULT_FALLBACK_CHAIN = "anthropic,gemini"

DEFAULT_ANTHROPIC_MODEL = "claude-opus-5"
DEFAULT_GEMINI_MODEL = "gemini-3.6-flash"

# The Messages API's effort levels, in order. Anything else is a typo, and a
# typo that reaches the API comes back as an opaque 400 mid-run.
EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")
DEFAULT_EFFORT = "high"

GEMINI_MAX_KEYS = 3

# Which backend and model actually produced the last successful summary. The
# document header records this, and on a fallback chain it's the only way to
# know after the fact which provider answered.
LAST_BACKEND = None
LAST_MODEL = None


def _record_used(backend, model):
    global LAST_BACKEND, LAST_MODEL
    LAST_BACKEND, LAST_MODEL = backend, model


class BackendUnavailable(RuntimeError):
    """This backend can't be used at all (no API key, missing SDK).

    Deliberately a normal exception rather than SystemExit: the fallback chain
    catches Exception, and SystemExit doesn't inherit from it. A missing key on
    the first backend used to kill the whole chain instead of advancing to the
    next one.
    """


@dataclass
class FrameMeta:
    """A single extracted frame from the recording."""
    timestamp_s: float
    kind: str  # "scene_change" or "periodic"
    path: str

    def label(self, idx):
        return f"[frame {idx} @ {self.timestamp_s:.1f}s ({self.kind})]"


def _max_tokens():
    try:
        return int(os.environ.get("SUMMARY_MAX_TOKENS", 16000))
    except ValueError:
        return 16000


def effort_level():
    """The configured effort, validated. Falls back to `high` with a warning."""
    value = (os.environ.get("SUMMARY_EFFORT") or DEFAULT_EFFORT).strip().lower()
    if value not in EFFORT_LEVELS:
        print(f"  warning: SUMMARY_EFFORT={value!r} is not one of "
              f"{', '.join(EFFORT_LEVELS)} — using {DEFAULT_EFFORT}",
              file=sys.stderr)
        return DEFAULT_EFFORT
    return value


def _read_image_b64(path):
    """Read an image file and return (base64_data, mime_type)."""
    p = Path(path)
    data = p.read_bytes()
    mime, _ = mimetypes.guess_type(str(p))
    if mime is None:
        # extract_frames.py emits .jpg, so this is the right default.
        mime = "image/jpeg"
    return base64.standard_b64encode(data).decode("ascii"), mime


def _render(frames, transcript, prompt_template):
    """Sort frames chronologically and fill in the prompt.

    Returns (sorted_frames, user_text). Every backend needs exactly this, and
    the frame order has to match the order the images are attached in.
    """
    sorted_frames = sorted(frames, key=lambda f: f.timestamp_s)
    manifest = "\n".join(f.label(i + 1) for i, f in enumerate(sorted_frames))
    user_text = prompt_template.format(transcript=transcript,
                                       frame_manifest=manifest)
    return sorted_frames, user_text


def _call_with_kwarg_fallback(func, label, base_kwargs, modern_kwargs):
    """Call `func`, moving `modern_kwargs` into extra_body on an old SDK.

    `output_config` and `thinking` are recent additions. An SDK that predates
    them raises TypeError for the unexpected keyword before any request is
    made, and `extra_body` passes them through on the wire unchanged — so the
    same code works on both without pinning a version.
    """
    try:
        return with_retries(func, label=label, **base_kwargs, **modern_kwargs)
    except TypeError as exc:
        if "unexpected keyword" not in str(exc):
            raise
        print(f"  note: installed SDK doesn't accept "
              f"{', '.join(modern_kwargs)} directly — passing via extra_body",
              file=sys.stderr)
        return with_retries(func, label=label, **base_kwargs,
                            extra_body=dict(modern_kwargs))


def summarize_anthropic(frames: List[FrameMeta], transcript: str,
                        prompt_template: str) -> str:
    """Anthropic Messages API — the default backend."""
    try:
        import anthropic
    except ImportError as exc:
        raise BackendUnavailable(f"anthropic SDK not installed: {exc}") from exc

    base_url = os.environ.get("ANTHROPIC_BASE_URL", "https://api.anthropic.com")
    model = (os.environ.get("ANTHROPIC_MODEL")
             or os.environ.get("SUMMARY_MODEL")
             or DEFAULT_ANTHROPIC_MODEL)

    # One key only — this is the account the operator pays for. The ring is
    # still used so ANTHROPIC_API_KEY_1 reads the same as ANTHROPIC_API_KEY.
    ring = KeyRing.from_env("ANTHROPIC_API_KEY", max_slots=1)
    if not ring:
        raise BackendUnavailable(missing_keys_message(
            "ANTHROPIC_API_KEY", 1,
            extra=("\nOnly one Anthropic key is used. Get one at "
                   "https://console.anthropic.com, or select a different "
                   "SUMMARY_BACKEND."),
        ))

    client = anthropic.Anthropic(base_url=base_url, api_key=ring.keys[0])
    sorted_frames, user_text = _render(frames, transcript, prompt_template)

    content = []
    for frame in sorted_frames:
        b64, mime = _read_image_b64(frame.path)
        content.append({
            "type": "image",
            "source": {"type": "base64", "media_type": mime, "data": b64},
        })
    content.append({"type": "text", "text": user_text})

    effort = effort_level()
    message = _call_with_kwarg_fallback(
        client.messages.create,
        f"anthropic/{model} (effort={effort})",
        {
            "model": model,
            "max_tokens": _max_tokens(),
            "messages": [{"role": "user", "content": content}],
        },
        {
            # Adaptive thinking: the model decides how much reasoning the
            # summary needs. budget_tokens is rejected by current models.
            "thinking": {"type": "adaptive"},
            "output_config": {"effort": effort},
        },
    )

    _record_used("anthropic", model)
    parts = [b.text for b in message.content if getattr(b, "type", None) == "text"]
    return "\n".join(parts).strip()


def summarize_gemini(frames: List[FrameMeta], transcript: str,
                     prompt_template: str) -> str:
    """Google Gemini via the google-genai SDK, rotating up to three keys.

    Rotation happens outside the retry wrapper on purpose: retry.py handles a
    provider that is busy, this loop handles a key that is exhausted or
    revoked. A key whose quota is gone would otherwise burn the full retry
    schedule before the chain ever moved on.
    """
    try:
        from google import genai as new_genai
    except ImportError as exc:
        raise BackendUnavailable(f"google-genai SDK not installed: {exc}") from exc

    model_name = os.environ.get("GEMINI_MODEL", DEFAULT_GEMINI_MODEL)
    ring = KeyRing.from_env("GEMINI_API_KEY", aliases=("GOOGLE_API_KEY",),
                            max_slots=GEMINI_MAX_KEYS)
    if not ring:
        raise BackendUnavailable(missing_keys_message(
            "GEMINI_API_KEY", GEMINI_MAX_KEYS,
            extra=("\nGOOGLE_API_KEY is also accepted. Get keys at "
                   "https://aistudio.google.com/apikey."),
        ))

    sorted_frames, user_text = _render(frames, transcript, prompt_template)
    parts = [{"text": user_text}]
    for frame in sorted_frames:
        data, mime = _read_image_b64(frame.path)
        parts.append({
            "inline_data": {
                "mime_type": mime,
                "data": base64.standard_b64decode(data),
            }
        })

    last_error = None
    for slot, key in ring.rotate():
        client = new_genai.Client(api_key=key)
        try:
            response = with_retries(
                client.models.generate_content,
                label=f"gemini/{model_name} ({ring.label(slot)})",
                model=model_name,
                contents=[{"role": "user", "parts": parts}],
            )
        except Exception as exc:  # noqa: BLE001 - try the next key
            if len(ring) == 1:
                raise
            print(f"  !! gemini {ring.label(slot)} failed: "
                  f"{type(exc).__name__}: {exc}", file=sys.stderr)
            last_error = exc
            continue
        ring.commit(slot)
        _record_used("gemini", model_name)
        return (response.text or "").strip()

    raise RuntimeError(
        f"All {len(ring)} Gemini key(s) failed. Last error: {last_error}")


# chain-name -> backend function
_BACKENDS = {
    "anthropic": summarize_anthropic,
    "claude": summarize_anthropic,
    "fcc": summarize_anthropic,   # alias kept for the user's older configs
    "gemini": summarize_gemini,
}


def summarize_with_fallback(frames: List[FrameMeta], transcript: str,
                            prompt_template: str) -> str:
    """Walk SUMMARY_FALLBACK_CHAIN in order; first backend to return wins.

    Each backend has already retried its own transient failures by the time it
    raises here, so reaching the next entry means that provider is genuinely
    unusable right now — not merely busy.

    Chain syntax: comma-separated names, e.g. "anthropic,gemini". Unknown
    entries are skipped with a warning; `disabled` is a sentinel for
    short-circuiting a chain without editing the env var.
    """
    chain_str = os.environ.get("SUMMARY_FALLBACK_CHAIN", DEFAULT_FALLBACK_CHAIN)
    chain = [name.strip().lower() for name in chain_str.split(",") if name.strip()]
    if not chain:
        raise RuntimeError("SUMMARY_FALLBACK_CHAIN is empty")

    failures = []
    for name in chain:
        if name == "disabled":
            continue
        func = _BACKENDS.get(name)
        if func is None:
            print(f"  warning: unknown backend {name!r} in chain - skipping",
                  file=sys.stderr)
            continue
        try:
            print(f"  -> trying {name}...")
            return func(frames, transcript, prompt_template)
        except BackendUnavailable as exc:
            print(f"  !! {name} unavailable: {exc}", file=sys.stderr)
            failures.append((name, exc))
        except Exception as exc:  # noqa: BLE001 - deliberately broad
            print(f"  !! {name} failed after retries: "
                  f"{type(exc).__name__}: {exc}", file=sys.stderr)
            failures.append((name, exc))

    history = "; ".join(f"{n}: {type(e).__name__}: {e}" for n, e in failures)
    raise RuntimeError(f"All {len(failures)} fallback backend(s) failed: {history}")


def summarize(frames: List[FrameMeta], transcript: str,
              prompt_template: str) -> str:
    """Dispatch to the configured backend."""
    backend = os.environ.get("SUMMARY_BACKEND", DEFAULT_BACKEND).lower()
    if backend == "fallback":
        return summarize_with_fallback(frames, transcript, prompt_template)
    func = _BACKENDS.get(backend)
    if func is None:
        raise SystemExit(
            f"Unknown SUMMARY_BACKEND: {backend!r} "
            f"(expected anthropic, gemini, or fallback)"
        )
    return func(frames, transcript, prompt_template)
