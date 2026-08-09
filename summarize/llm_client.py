#!/usr/bin/env python3
"""
Pluggable LLM client for the meeting-summary agent.

Four single-backend modes plus an auto-fallback chain, all selected by env var:

  Auto-fallback (`SUMMARY_BACKEND=fallback`, the default) - walks
  `SUMMARY_FALLBACK_CHAIN` in order. Each entry is one of: `gemini`, `fcc`
  (alias for anthropic), `anthropic`, `nvidia_nim`, `ollama`. A backend is only
  abandoned after its own retries are exhausted; the first to return wins. If
  every backend fails, the accumulated history is raised. Default chain:
  `gemini,fcc,nvidia_nim`.

  Google Gemini (`SUMMARY_BACKEND=gemini`) - the google-genai SDK. Reads
  `GEMINI_MODEL` and `GOOGLE_API_KEY` / `GEMINI_API_KEY`.

  Anthropic SDK (`SUMMARY_BACKEND=anthropic`) - works against both
  api.anthropic.com and any proxy that speaks the Messages API (FCC, LiteLLM),
  because the SDK honors ANTHROPIC_BASE_URL.

  NVIDIA NIM (`SUMMARY_BACKEND=nvidia_nim`) - OpenAI-compatible chat
  completions against integrate.api.nvidia.com or a self-hosted NIM.

  Ollama (`SUMMARY_BACKEND=ollama`) - /api/generate with a vision-capable local
  model (e.g. llava:13b). The original local-only option.

TRANSIENT FAILURES. Every backend's network call goes through
summarize/retry.py: 503 "server is busy", 429, 5xx and connection errors are
retried with exponential backoff and full jitter (honoring Retry-After), and
only a backend that keeps failing hands over to the next in the chain. See
retry.py for the policy and its env vars.

MISSING CREDENTIALS raise BackendUnavailable, which the chain treats as "skip
this one" rather than a fatal error — a chain of three backends shouldn't die
because the first one's key isn't configured.

All four vision-aware backends take the same extracted frames; the prompt and
frame-list shape don't vary by provider.

Env vars:
  SUMMARY_BACKEND       "fallback" (default), "gemini", "anthropic",
                        "nvidia_nim", or "ollama"
  SUMMARY_FALLBACK_CHAIN  default "gemini,fcc,nvidia_nim"
  GEMINI_MODEL          default gemini-2.5-flash
  GOOGLE_API_KEY        required for gemini (GEMINI_API_KEY also accepted)
  ANTHROPIC_BASE_URL    default https://api.anthropic.com
  ANTHROPIC_API_KEY     required for anthropic / fcc
  SUMMARY_MODEL         model for the anthropic backend
                        (default: claude-sonnet-4-5)
  NVIDIA_NIM_BASE_URL   default https://integrate.api.nvidia.com/v1
  NVIDIA_NIM_API_KEY    required for nvidia_nim
  NVIDIA_NIM_MODEL      default meta/llama-3.1-70b-instruct
  OLLAMA_HOST           default http://localhost:11434
  OLLAMA_MODEL          default llava:13b
  SUMMARY_MAX_TOKENS    default 4096
"""
import base64
import json
import mimetypes
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import List

sys.path.insert(0, str(Path(__file__).resolve().parent))

from retry import with_retries  # noqa: E402

DEFAULT_BACKEND = "fallback"
DEFAULT_FALLBACK_CHAIN = "gemini,fcc,nvidia_nim"

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
        return int(os.environ.get("SUMMARY_MAX_TOKENS", 4096))
    except ValueError:
        return 4096


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


def summarize_anthropic(frames: List[FrameMeta], transcript: str,
                        prompt_template: str) -> str:
    """Anthropic Messages API — Anthropic direct, FCC, or any compatible proxy."""
    try:
        import anthropic
    except ImportError as exc:
        raise BackendUnavailable(f"anthropic SDK not installed: {exc}") from exc

    base_url = os.environ.get("ANTHROPIC_BASE_URL", "https://api.anthropic.com")
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    model = os.environ.get("SUMMARY_MODEL", "claude-sonnet-4-5")

    if not api_key:
        env_file = Path(__file__).resolve().parent.parent / ".env"
        raise BackendUnavailable(
            "ANTHROPIC_API_KEY is not set. "
            f"Looked in: process environment, {env_file}, /etc/meeting-bot.env. "
            "Add ANTHROPIC_API_KEY=<key> to .env, or select a different "
            "SUMMARY_BACKEND — only anthropic/fcc need this key."
        )

    client = anthropic.Anthropic(base_url=base_url, api_key=api_key)
    sorted_frames, user_text = _render(frames, transcript, prompt_template)

    content = []
    for frame in sorted_frames:
        b64, mime = _read_image_b64(frame.path)
        content.append({
            "type": "image",
            "source": {"type": "base64", "media_type": mime, "data": b64},
        })
    content.append({"type": "text", "text": user_text})

    message = with_retries(
        client.messages.create,
        label=f"anthropic/{model}",
        model=model,
        max_tokens=_max_tokens(),
        messages=[{"role": "user", "content": content}],
    )

    _record_used("anthropic", model)
    parts = [b.text for b in message.content if getattr(b, "type", None) == "text"]
    return "\n".join(parts).strip()


def summarize_ollama(frames: List[FrameMeta], transcript: str,
                     prompt_template: str) -> str:
    """Local Ollama server with a vision-capable model."""
    import urllib.request

    host = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
    model = os.environ.get("OLLAMA_MODEL", "llava:13b")

    sorted_frames, user_text = _render(frames, transcript, prompt_template)
    images = [_read_image_b64(f.path)[0] for f in sorted_frames]

    payload = {
        "model": model,
        "prompt": user_text,
        "images": images,
        "stream": False,
    }

    def _call():
        req = urllib.request.Request(
            f"{host}/api/generate",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        # Without a timeout a wedged local model hangs the stage forever, and
        # the retry wrapper never gets a chance to do anything.
        with urllib.request.urlopen(req, timeout=600) as resp:
            return json.loads(resp.read().decode("utf-8"))

    result = with_retries(_call, label=f"ollama/{model}")
    _record_used("ollama", model)
    return result.get("response", "").strip()


def summarize_nim(frames: List[FrameMeta], transcript: str,
                  prompt_template: str) -> str:
    """NVIDIA NIM, or any OpenAI-compatible chat-completions endpoint."""
    try:
        import openai
    except ImportError as exc:
        raise BackendUnavailable(f"openai SDK not installed: {exc}") from exc

    base_url = os.environ.get("NVIDIA_NIM_BASE_URL",
                              "https://integrate.api.nvidia.com/v1")
    api_key = os.environ.get("NVIDIA_NIM_API_KEY")
    model = os.environ.get("NVIDIA_NIM_MODEL", "meta/llama-3.1-70b-instruct")

    if not api_key:
        raise BackendUnavailable(
            "NVIDIA_NIM_API_KEY is not set (needed for the nvidia_nim backend)"
        )

    client = openai.OpenAI(base_url=base_url, api_key=api_key)
    sorted_frames, user_text = _render(frames, transcript, prompt_template)

    content = []
    for frame in sorted_frames:
        b64, mime = _read_image_b64(frame.path)
        content.append({
            "type": "image_url",
            "image_url": {"url": f"data:{mime};base64,{b64}"},
        })
    content.append({"type": "text", "text": user_text})

    response = with_retries(
        client.chat.completions.create,
        label=f"nvidia_nim/{model}",
        model=model,
        messages=[{"role": "user", "content": content}],
        max_tokens=_max_tokens(),
        temperature=0.2,
    )

    if not response.choices:
        raise RuntimeError("NVIDIA NIM returned no choices")
    _record_used("nvidia_nim", model)
    return (response.choices[0].message.content or "").strip()


def summarize_gemini(frames: List[FrameMeta], transcript: str,
                     prompt_template: str) -> str:
    """Google Gemini via the google-genai SDK.

    Only the new `google-genai` SDK is wired up; the legacy
    `google.generativeai` import path is no longer supported. Accepts
    GOOGLE_API_KEY or GEMINI_API_KEY, whichever the operator already has set.
    """
    try:
        from google import genai as new_genai
    except ImportError as exc:
        raise BackendUnavailable(f"google-genai SDK not installed: {exc}") from exc

    api_key = os.environ.get("GOOGLE_API_KEY") or os.environ.get("GEMINI_API_KEY")
    model_name = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")

    if not api_key:
        raise BackendUnavailable(
            "GOOGLE_API_KEY (or GEMINI_API_KEY) is not set "
            "(needed for the gemini backend)"
        )

    sorted_frames, user_text = _render(frames, transcript, prompt_template)

    client = new_genai.Client(api_key=api_key)
    parts = [{"text": user_text}]
    for frame in sorted_frames:
        data, mime = _read_image_b64(frame.path)
        parts.append({
            "inline_data": {
                "mime_type": mime,
                "data": base64.standard_b64decode(data),
            }
        })

    response = with_retries(
        client.models.generate_content,
        label=f"gemini/{model_name}",
        model=model_name,
        contents=[{"role": "user", "parts": parts}],
    )
    _record_used("gemini", model_name)
    return (response.text or "").strip()


# chain-name -> backend function
_BACKENDS = {
    "anthropic": summarize_anthropic,
    "fcc": summarize_anthropic,   # alias kept for the user's preferred config
    "ollama": summarize_ollama,
    "nvidia_nim": summarize_nim,
    "gemini": summarize_gemini,
}


def summarize_with_fallback(frames: List[FrameMeta], transcript: str,
                            prompt_template: str) -> str:
    """Walk SUMMARY_FALLBACK_CHAIN in order; first backend to return wins.

    Each backend has already retried its own transient failures by the time it
    raises here, so reaching the next entry means that provider is genuinely
    unusable right now — not merely busy.

    Chain syntax: comma-separated names, e.g. "gemini,fcc,nvidia_nim". Unknown
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
            f"(expected gemini, anthropic, nvidia_nim, ollama, or fallback)"
        )
    return func(frames, transcript, prompt_template)
