#!/usr/bin/env python3
"""
Pluggable LLM client for the meeting-summary agent.

Four single-backend modes plus an auto-fallback chain. All selected via env
vars:

  Google Gemini (`SUMMARY_BACKEND=gemini`, the default) - uses the
  google-genai SDK. Reads `GEMINI_MODEL` (and `GOOGLE_API_KEY` /
  `GEMINI_API_KEY`).

  Anthropic SDK (`SUMMARY_BACKEND=anthropic`) - works against both
  api.anthropic.com and any OpenAI-compatible proxy (FCC, LiteLLM,
  etc.) because the SDK honors ANTHROPIC_BASE_URL.

  Ollama (`SUMMARY_BACKEND=ollama`) - hits /api/chat with a vision-capable
  local model (e.g. llava:13b). The original local-only option.

  NVIDIA NIM (`SUMMARY_BACKEND=nvidia_nim`) - OpenAI-compatible chat
  completions against integrate.api.nvidia.com (or a self-hosted NIM). The
  Python `openai` SDK handles both the cloud and self-hosted cases.

  Auto-fallback (`SUMMARY_BACKEND=fallback`) - walks `SUMMARY_FALLBACK_CHAIN`
  in order. Each entry is one of: `fcc` (alias for anthropic), `anthropic`,
  `nvidia_nim`, `gemini`, `ollama`. On any exception, the next entry is tried;
  the first one to return wins. If every backend fails, the last exception
  is re-raised. Default chain: `fcc,nvidia_nim,gemini`.

All four vision-aware backends accept the same extracted frames - the
prompt + frame list shape is the same regardless of provider.

Env vars:
  ANTHROPIC_BASE_URL    default https://api.anthropic.com
  ANTHROPIC_API_KEY     required when using anthropic / fcc / fallback chain
  SUMMARY_MODEL         model name passed to the active backend
                         (default: claude-sonnet-4-5)
  SUMMARY_BACKEND       "gemini" (default), "anthropic", "ollama",
                         "nvidia_nim", or "fallback"
  OLLAMA_HOST           default http://localhost:11434
  OLLAMA_MODEL          default llava:13b (ollama backend only)
  NVIDIA_NIM_BASE_URL   default https://integrate.api.nvidia.com/v1
  NVIDIA_NIM_API_KEY    required when using nvidia_nim / fallback
  NVIDIA_NIM_MODEL      default meta/llama-3.1-70b-instruct
  GEMINI_MODEL          default gemini-2.5-flash
  GOOGLE_API_KEY        required when using gemini / fallback
                         (GEMINI_API_KEY is also accepted)
  SUMMARY_FALLBACK_CHAIN  default "fcc,nvidia_nim,gemini"
"""
import base64
import json
import mimetypes
import os
from dataclasses import dataclass
from pathlib import Path
from typing import List


@dataclass
class FrameMeta:
    """A single extracted frame from the recording."""
    timestamp_s: float
    kind: str  # "scene_change" or "periodic"
    path: str

    def label(self, idx):
        return f"[frame {idx} @ {self.timestamp_s:.1f}s ({self.kind})]"


def _read_image_b64(path):
    """Read an image file and return (base64_data, mime_type)."""
    p = Path(path)
    data = p.read_bytes()
    mime, _ = mimetypes.guess_type(str(p))
    if mime is None:
        # Default to JPEG since extract_frames.py emits .jpg files.
        mime = "image/jpeg"
    return base64.standard_b64encode(data).decode("ascii"), mime


def summarize_anthropic(frames: List[FrameMeta], transcript: str,
                        prompt_template: str) -> str:
    """Call the Anthropic SDK. Works for Anthropic direct + FCC + any
    OpenAI-compatible proxy that accepts the Messages API."""
    import anthropic

    base_url = os.environ.get("ANTHROPIC_BASE_URL", "https://api.anthropic.com")
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    model = os.environ.get("SUMMARY_MODEL", "claude-sonnet-4-5")

    if not api_key:
        # Loud diagnostic so the operator can see exactly what's missing and
        # where to set it. The previous one-line message masked the resolution
        # order — every "ANTHROPIC_API_KEY env var must be set" hit needed a
        # second grep to figure out which key was actually missing.
        _env_file_hint = (
            Path(__file__).resolve().parent.parent / ".env"
        )
        raise SystemExit(
            "ANTHROPIC_API_KEY is not set.\n"
            f"  Looked in: process environment, {_env_file_hint}, /etc/meeting-bot.env.\n"
            "  Get a key from https://console.anthropic.com or your FCC proxy.\n"
            "  Then either:\n"
            "    - add ANTHROPIC_API_KEY=<key> to .env and re-run, or\n"
            "    - export ANTHROPIC_API_KEY=<key> before invoking pipeline.sh.\n"
            "  If you intended a different SUMMARY_BACKEND (ollama, gemini,\n"
            "  nvidia_nim), set that instead — only anthropic/fcc need this key."
        )

    client = anthropic.Anthropic(base_url=base_url, api_key=api_key)

    # Build the user message. The Anthropic SDK accepts image content blocks
    # directly (base64 source). Order frames by timestamp so the model sees
    # them chronologically.
    content = []
    manifest_lines = []
    for i, frame in enumerate(sorted(frames, key=lambda f: f.timestamp_s)):
        b64, mime = _read_image_b64(frame.path)
        content.append({
            "type": "image",
            "source": {"type": "base64", "media_type": mime, "data": b64},
        })
        manifest_lines.append(frame.label(i + 1))

    frame_manifest_str = "\n".join(manifest_lines)
    user_text = prompt_template.format(
        transcript=transcript,
        frame_manifest=frame_manifest_str,
    )

    content.append({"type": "text", "text": user_text})

    message = client.messages.create(
        model=model,
        max_tokens=4096,
        messages=[{"role": "user", "content": content}],
    )

    # The SDK returns content as a list of blocks; concatenate any text blocks.
    parts = []
    for block in message.content:
        if getattr(block, "type", None) == "text":
            parts.append(block.text)
    return "\n".join(parts).strip()


def summarize_ollama(frames: List[FrameMeta], transcript: str,
                     prompt_template: str) -> str:
    """Call a local Ollama server with a vision-capable model."""
    import urllib.request

    host = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
    model = os.environ.get("OLLAMA_MODEL", "llava:13b")

    content = []
    manifest_lines = []
    for i, frame in enumerate(sorted(frames, key=lambda f: f.timestamp_s)):
        b64, _ = _read_image_b64(frame.path)
        content.append(b64)
        manifest_lines.append(frame.label(i + 1))

    frame_manifest_str = "\n".join(manifest_lines)
    user_text = prompt_template.format(
        transcript=transcript,
        frame_manifest=frame_manifest_str,
    )

    payload = {
        "model": model,
        "prompt": user_text,
        "images": content,
        "stream": False,
    }

    req = urllib.request.Request(
        f"{host}/api/generate",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req) as resp:
        result = json.loads(resp.read().decode("utf-8"))
    return result.get("response", "").strip()


def summarize_nim(frames: List[FrameMeta], transcript: str,
                  prompt_template: str) -> str:
    """Call NVIDIA NIM (or any OpenAI-compatible chat-completions endpoint).

    Uses the `openai` Python SDK which works against both the hosted NVIDIA
    NIM (integrate.api.nvidia.com) and self-hosted NIM deployments. Vision
    input is sent as image_url content blocks (data: URI, base64)."""
    import openai

    base_url = os.environ.get(
        "NVIDIA_NIM_BASE_URL", "https://integrate.api.nvidia.com/v1"
    )
    api_key = os.environ.get("NVIDIA_NIM_API_KEY")
    model = os.environ.get(
        "NVIDIA_NIM_MODEL", "meta/llama-3.1-70b-instruct"
    )

    if not api_key:
        raise SystemExit(
            "NVIDIA_NIM_API_KEY env var must be set when "
            "SUMMARY_BACKEND=nvidia_nim"
        )

    client = openai.OpenAI(base_url=base_url, api_key=api_key)

    content = []
    manifest_lines = []
    for i, frame in enumerate(sorted(frames, key=lambda f: f.timestamp_s)):
        b64, mime = _read_image_b64(frame.path)
        content.append({
            "type": "image_url",
            "image_url": {"url": f"data:{mime};base64,{b64}"},
        })
        manifest_lines.append(frame.label(i + 1))

    frame_manifest_str = "\n".join(manifest_lines)
    user_text = prompt_template.format(
        transcript=transcript,
        frame_manifest=frame_manifest_str,
    )
    content.append({"type": "text", "text": user_text})

    response = client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": content}],
        max_tokens=4096,
        temperature=0.2,
    )

    if not response.choices:
        raise RuntimeError("NVIDIA NIM returned no choices")
    return (response.choices[0].message.content or "").strip()


def summarize_gemini(frames: List[FrameMeta], transcript: str,
                     prompt_template: str) -> str:
    """Call Google Gemini via the google-genai SDK.

    Only the new `google-genai` SDK is wired up — setup.sh installs that
    package and the legacy `google.generativeai` import path is no longer
    supported. Accepts GOOGLE_API_KEY or GEMINI_API_KEY for compatibility
    with whichever the user already has configured. Frames are sent as
    inline image parts.
    """
    from google import genai as new_genai

    api_key = os.environ.get("GOOGLE_API_KEY") or os.environ.get("GEMINI_API_KEY")
    model_name = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")

    if not api_key:
        raise SystemExit(
            "GOOGLE_API_KEY (or GEMINI_API_KEY) must be set when "
            "SUMMARY_BACKEND=gemini"
        )

    # Manifest lines in the same order as the image parts.
    sorted_frames = sorted(frames, key=lambda f: f.timestamp_s)
    manifest_lines = [frame.label(i + 1) for i, frame in enumerate(sorted_frames)]
    frame_manifest_str = "\n".join(manifest_lines)
    user_text = prompt_template.format(
        transcript=transcript,
        frame_manifest=frame_manifest_str,
    )

    client = new_genai.Client(api_key=api_key)
    parts = [{"text": user_text}]
    import base64 as _b64
    for frame in sorted_frames:
        data, mime = _read_image_b64(frame.path)
        parts.append({
            "inline_data": {
                "mime_type": mime,
                "data": _b64.standard_b64decode(data),
            }
        })
    response = client.models.generate_content(
        model=model_name,
        contents=[{"role": "user", "parts": parts}],
    )
    return (response.text or "").strip()


# Map of chain-name -> backend function. Used by summarize_with_fallback.
_BACKENDS = {
    "anthropic": summarize_anthropic,
    "fcc": summarize_anthropic,   # alias kept for the user's preferred config
    "ollama": summarize_ollama,
    "nvidia_nim": summarize_nim,
    "gemini": summarize_gemini,
}

DEFAULT_FALLBACK_CHAIN = "fcc,nvidia_nim,gemini"


def summarize_with_fallback(frames: List[FrameMeta], transcript: str,
                            prompt_template: str) -> str:
    """Walk SUMMARY_FALLBACK_CHAIN in order. Returns the first backend's
    response; logs each failure and advances on any exception. Raises the
    last exception (with an appended history) if the entire chain fails.

    Chain syntax: comma-separated names, e.g. "fcc,nvidia_nim,gemini".
    Skips unknown entries with a warning. `disabled` is a sentinel that
    lets you short-circuit a chain without editing the env var."""
    chain_str = os.environ.get("SUMMARY_FALLBACK_CHAIN", DEFAULT_FALLBACK_CHAIN)
    chain = [name.strip().lower() for name in chain_str.split(",") if name.strip()]
    if not chain:
        raise SystemExit("SUMMARY_FALLBACK_CHAIN is empty")

    failures = []
    for name in chain:
        func = _BACKENDS.get(name)
        if name == "disabled":
            continue
        if func is None:
            print(f"  warning: unknown backend {name!r} in chain - skipping",
                  file=__import__("sys").stderr)
            continue
        try:
            print(f"  -> trying {name}...")
            return func(frames, transcript, prompt_template)
        except Exception as e:  # noqa: BLE001 - deliberately broad
            print(f"  !! {name} failed: {type(e).__name__}: {e}",
                  file=__import__("sys").stderr)
            failures.append((name, e))

    # Out of retries. Surface whatever we accumulated.
    history = "; ".join(f"{n}: {type(e).__name__}: {e}" for n, e in failures)
    raise SystemExit(
        f"All {len(failures)} fallback backend(s) failed: {history}"
    )


def summarize(frames: List[FrameMeta], transcript: str,
              prompt_template: str) -> str:
    """Dispatch to the configured backend."""
    backend = os.environ.get("SUMMARY_BACKEND", "gemini").lower()
    if backend == "fallback":
        return summarize_with_fallback(frames, transcript, prompt_template)
    if backend in ("anthropic", "fcc"):
        return summarize_anthropic(frames, transcript, prompt_template)
    if backend == "ollama":
        return summarize_ollama(frames, transcript, prompt_template)
    if backend == "nvidia_nim":
        return summarize_nim(frames, transcript, prompt_template)
    if backend == "gemini":
        return summarize_gemini(frames, transcript, prompt_template)
    raise SystemExit(
        f"Unknown SUMMARY_BACKEND: {backend!r} "
        f"(expected anthropic, ollama, nvidia_nim, gemini, or fallback)"
    )