#!/usr/bin/env python3
"""
Pluggable LLM client for the meeting-summary agent.

Two backends plus an auto-fallback chain, all selected by env var:

  Auto-fallback (`SUMMARY_BACKEND=fallback`, the default) - walks
  `SUMMARY_FALLBACK_CHAIN` in order. Each entry is one of: `claude-cli`
  (aliases: `claude`, `anthropic`, `fcc`) or `gemini`. A backend is only
  abandoned after its own retries are exhausted; the first to return wins. If
  every backend fails, the accumulated history is raised. Default chain:
  `claude-cli,gemini`.

  Claude via the Claude Code CLI (`SUMMARY_BACKEND=claude-cli`) - the default
  and primary backend. Runs `claude -p` as a subprocess, so the summary is
  billed to the operator's **Claude subscription** (the account `claude auth`
  is logged into), not to a metered API key. Reads CLAUDE_CLI_MODEL (default
  `opus`) and SUMMARY_EFFORT.

  Google Gemini (`SUMMARY_BACKEND=gemini`) - the google-genai SDK, with up to
  three keys rotated round-robin (GEMINI_API_KEY_1..3).

WHY A SUBPROCESS AND NOT THE MESSAGES API. A Claude Pro/Max subscription has
no API key; api.anthropic.com bills per token against a separate console
account. The CLI is the supported way to spend a subscription
non-interactively, so the backend shells out to it rather than importing the
`anthropic` SDK. There is deliberately no ANTHROPIC_API_KEY path left in this
file - see CLAUDE.md.

THE SUBPROCESS ENVIRONMENT IS SCRUBBED. ANTHROPIC_API_KEY, ANTHROPIC_AUTH_TOKEN
and ANTHROPIC_BASE_URL are removed before the CLI is launched. If any of them
survives, the CLI silently switches from subscription auth to API-key billing -
the summary still appears, and the charge lands on an account the operator
thought was unused. An *empty* ANTHROPIC_API_KEY is worse still: it fails
authentication outright, which looks like a broken subscription.

FRAMES ARE READ FROM DISK, NOT INLINED. The Messages API takes base64 image
blocks; `claude -p` takes a prompt string. So the frame manifest carries
absolute paths, the CLI is given the Read tool restricted to the frame
directories (--tools Read --allowedTools Read --add-dir), and the model opens
the images itself. Set CLAUDE_CLI_FRAME_VISION=0 to send the manifest as text
only - faster and cheaper against a subscription's rate limit, but then the
model cites frames it has never seen.

A CACHE-STABLE PREFIX. A prompt template may fence the half that never varies
between runs (role, instructions, output format, worked example) with
`<!-- static-prompt: begin -->` / `<!-- static-prompt: end -->`. This backend
lifts that half out and passes it to the CLI as
`--append-system-prompt-file <content-addressed path>`, adding
`--exclude-dynamic-system-prompt-sections` so the CLI's own per-machine
sections (cwd, date, git status) move out of the system prompt too. Everything
that varies - the chunk label mapreduce prepends, the reference material, the
transcript, the frame paths - stays in the piped user turn. The result is a
byte-identical prefix across runs and across the chunks of one run, which is
what Claude's automatic prompt caching needs. There is no manual cache_control
flag on the CLI; caching is automatic, and this is the only lever we have.
A template without the markers is sent exactly as it always was.

EFFORT, NOT A TOKEN BUDGET. SUMMARY_EFFORT maps onto the CLI's `--effort`
(low | medium | high | xhigh | max), the same scale the Messages API exposes as
`output_config.effort`. Thinking is adaptive: the model decides when to use it.
There is no token-budget knob, deliberately — and SUMMARY_MAX_TOKENS does NOT
apply here: the CLI has no output cap flag, and output is not where a
subscription window goes anyway. What actually spends it is input — the
frames the model opens (~790 tokens each at 1024px), the transcript, and the
thinking `--effort` buys. The levers that exist are CLAUDE_CLI_MAX_FRAMES
(frames offered per call), FRAME_MAX_DIMENSION, CLAUDE_CLI_FRAME_VISION,
SUMMARY_EFFORT and CLAUDE_CLI_MODEL.

THE WINDOW IS METERED, AND A HIT WINDOW WAITS. `--output-format stream-json`
makes the CLI emit a `rate_limit_event` beside the result, carrying the
subscription's own 5-hour and 7-day meters (`unifiedWindows.five_hour.
{utilization, resetsAt}`). Every call's token usage and the meter before and
after it go into `USAGE`, which summarize.py writes into the run's state.json,
so "how much of the window did that lecture cost" is a number on disk rather
than a guess. When the window is exhausted the event says `rejected` with the
reset time; that becomes ClaudeCliRateLimited, which is neither retried on
the backoff schedule nor handed to Gemini — the call sleeps until the reset
(CLAUDE_CLI_MAX_WAIT_SECONDS, default 6h, caps that) and tries again, so the
summary stays on the subscription. Past the cap the stage fails with the
reset time recorded, and `pipeline.sh --resume-all` (from a timer, or by
hand) picks it up once the window has reset.

TRANSIENT FAILURES. Every backend's network call goes through
summarize/retry.py: 503 "server is busy", 429, 5xx and connection errors are
retried with exponential backoff and full jitter (honoring Retry-After), and
only a backend that keeps failing hands over to the next in the chain. The CLI
reports these as text rather than as HTTP status codes, so ClaudeCliError
carries the CLI's own wording and retry.py's pattern matcher classifies it.

MISSING CREDENTIALS raise BackendUnavailable, which the chain treats as "skip
this one" rather than a fatal error - a chain of two backends shouldn't die
because the first one isn't logged in.

Both backends take the same extracted frames; the prompt and frame-list shape
don't vary by provider.

Env vars:
  SUMMARY_BACKEND       "fallback" (default), "claude-cli", or "gemini"
  SUMMARY_FALLBACK_CHAIN  default "claude-cli,gemini"
  CLAUDE_CLI_BIN        path to the claude binary (default: found on PATH)
  CLAUDE_CLI_MODEL      default "opus" (ANTHROPIC_MODEL also accepted)
  CLAUDE_CLI_TIMEOUT_SECONDS  default 1800
  CLAUDE_CLI_FRAME_VISION  1 (default) lets the model Read the frame images
  CLAUDE_CLI_STATIC_PROMPT 1 (default) hands the unchanging instructions to
                        the CLI as a system prompt file; 0 sends them inline
  FRAME_MAX_DIMENSION   long edge, px, of the frame copies sent to the CLI
                        (default 1024; 0 sends the originals)
  CLAUDE_CLI_MAX_FRAMES most frames offered to the model per call (default 0
                        = every frame of the chunk); scene changes are kept
                        first, periodic frames are thinned evenly
  CLAUDE_CLI_MAX_WAIT_SECONDS  how long one call may sleep for the usage
                        window to reset before the stage fails (default 21600)
  CLAUDE_CLI_RATE_LIMIT_POLL_SECONDS  retry interval when the CLI reports a
                        hit window without a reset time (default 600)
  SUMMARY_EFFORT        low | medium | high (default) | xhigh | max
  GEMINI_API_KEY_1..3   required for gemini (GOOGLE_API_KEY also accepted)
  GEMINI_MODEL          default gemini-3.6-flash
  SUMMARY_MAX_TOKENS    default 16000 (gemini only; the CLI has no such flag)
"""
import base64
import hashlib
import json
import mimetypes
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import List

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))

from retry import with_retries  # noqa: E402
from keyring import KeyRing, missing_keys_message  # noqa: E402

DEFAULT_BACKEND = "fallback"
DEFAULT_FALLBACK_CHAIN = "claude-cli,gemini"

# An alias, not a pinned id: the CLI resolves "opus" to the current Opus, so a
# model rename doesn't turn into a 404 on a box nobody has touched in a year.
DEFAULT_CLAUDE_CLI_MODEL = "opus"
DEFAULT_CLAUDE_CLI_TIMEOUT = 1800
DEFAULT_GEMINI_MODEL = "gemini-3.6-flash"

# The effort levels the CLI's --effort accepts, in order. Anything else is a
# typo, and a typo that reaches the CLI comes back as an opaque usage error.
EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")
DEFAULT_EFFORT = "high"

GEMINI_MAX_KEYS = 3

# Env vars that would flip the CLI from subscription auth to API-key billing.
# Scrubbed from the subprocess environment, never from our own.
_API_KEY_VARS = ("ANTHROPIC_API_KEY", "ANTHROPIC_API_KEY_1",
                 "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL")

# Wording the CLI uses when it has no usable subscription. These mean "this
# backend cannot work at all", not "the service is busy" — so they become
# BackendUnavailable and the chain advances immediately instead of burning the
# full retry schedule on something no retry can fix.
_NOT_LOGGED_IN_MARKERS = (
    "not logged in",
    "please run /login",
    "invalid api key",
    "authentication_error",
    "oauth token has expired",
    "oauth session expired",
    "failed to authenticate",
    "credit balance is too low",
)

# Delimiters a prompt template may use to mark the block that never varies
# between runs (role, instructions, output format, worked example). The
# claude-cli backend lifts that block out of the piped prompt and passes it as
# a system prompt instead, so the reusable prefix stays byte-identical across
# runs and chunks and Claude's automatic prompt caching can hit it. A template
# without the markers is sent exactly as it always was.
STATIC_PROMPT_BEGIN = "<!-- static-prompt: begin -->"
STATIC_PROMPT_END = "<!-- static-prompt: end -->"

# Long edge, in pixels, of the frame copies handed to the CLI. A 1920x1080
# keyframe costs roughly 1,844 tokens once the model rescales it; 1024px is
# about 790, and a slide is still legible. 0 disables the downscale.
DEFAULT_FRAME_MAX_DIMENSION = 1024
# Where the downscaled copies go, relative to the directory the originals are
# in. Inside FRAMES_DIR on purpose: those are the disposable artifacts, and
# keeping the copies under the same parent means --add-dir already covers them.
LLM_FRAME_SUBDIR = "llm-{max_dim}"

# Per-call frame cap. 0 = no cap, which is what every run did before the
# setting existed. The frames are the bulk of a call's input, so this is the
# first thing to turn down when a lecture doesn't fit the subscription window.
DEFAULT_MAX_FRAMES = 0

# A hit usage window: how long one call may sleep for the reset before giving
# up (6h covers a full 5-hour window plus margin — anything longer is the
# weekly limit, which no wait inside a stage should sit through), how often
# to try again when the CLI names no reset time, and the margin added to the
# reset time it does name so the retry doesn't land a second early.
DEFAULT_MAX_WAIT_SECONDS = 6 * 3600
DEFAULT_RATE_LIMIT_POLL_SECONDS = 600
RATE_LIMIT_MARGIN_SECONDS = 60

# Wording the CLI uses for an exhausted subscription window when the
# machine-readable signals (a `rate_limit_event` with status "rejected", or
# `api_error_status` 429) are absent — an older CLI, or `--output-format json`.
# The oldest form is "Claude AI usage limit reached|<unix reset time>".
_RATE_LIMIT_RE = re.compile(
    r"(usage limit reached|hit your (?:\w+ )?limit|rate limit)", re.IGNORECASE)
_RATE_LIMIT_RESET_RE = re.compile(r"limit reached\|(\d{9,11})")

# Sleeps go through this so the unit tests can stand in for the clock; the
# wait for a window reset would otherwise take hours to test.
_sleep = time.sleep

# summarize.py installs a callable here to mirror a wait into the run's
# state.json (`waiting_until`), so `pipeline.sh --status` can say why a
# summarize stage has been "running" for three hours. Called with keyword
# arguments; see _wait_for_window. None means nobody is listening.
WAIT_HOOK = None

# Which backend and model actually produced the last successful summary. The
# document header records this, and on a fallback chain it's the only way to
# know after the fact which provider answered.
LAST_BACKEND = None
LAST_MODEL = None


def _record_used(backend, model):
    global LAST_BACKEND, LAST_MODEL
    LAST_BACKEND, LAST_MODEL = backend, model


class BackendUnavailable(RuntimeError):
    """This backend can't be used at all (not logged in, CLI missing, no key).

    Deliberately a normal exception rather than SystemExit: the fallback chain
    catches Exception, and SystemExit doesn't inherit from it. A missing
    credential on the first backend used to kill the whole chain instead of
    advancing to the next one.

    `retryable = False` is load-bearing. This is raised from inside
    with_retries (the CLI only reveals "not logged in" once it has run), and
    retry.py's type-name heuristic would otherwise read "Unavailable" as a busy
    server and sit through the full backoff schedule before advancing.
    """

    retryable = False


class ClaudeCliError(RuntimeError):
    """The CLI ran but reported a failure.

    The message is the CLI's own text, kept verbatim so retry.is_retryable can
    classify it — the CLI has no HTTP status to expose, so its wording
    ("overloaded", "rate limit") is the only signal available.
    """


class ClaudeCliRateLimited(ClaudeCliError):
    """The subscription's usage window is exhausted.

    Not a transient failure and not an unusable backend, so it is handled by
    neither of the two existing paths: `retryable = False` keeps with_retries
    from burning its backoff schedule on it, and summarize_with_fallback
    re-raises it instead of advancing to Gemini — the operator chose to wait
    for the subscription rather than pay a second provider. The wait itself is
    _wait_for_window; when that gives up, `pause_run` tells mapreduce to fail
    the stage (resumable, reset time recorded) rather than merge around the
    missing chunk and ship a document with a hole in it.

    `resets_at` is a unix timestamp when the CLI reported one, else None.
    `window` is the CLI's name for the limit ("five_hour", "seven_day", ...).
    """

    retryable = False
    pause_run = True

    def __init__(self, message, resets_at=None, window=None):
        super().__init__(message)
        self.resets_at = resets_at
        self.window = window


class UsageLedger:
    """Every claude-cli call's token usage plus the subscription meter.

    Thread-safe because chunks are summarized in parallel. `summary()` is what
    summarize.py writes into state.json at the end of the stage — including a
    stage that fails partway, since the calls that did complete were spent.
    """

    _COUNTERS = ("input_tokens", "output_tokens", "cache_read_input_tokens",
                 "cache_creation_input_tokens", "thinking_tokens")

    def __init__(self):
        self._lock = threading.Lock()
        self.reset()

    def reset(self):
        with self._lock:
            self.calls = []
            self.first_windows = None
            self.last_windows = None

    @staticmethod
    def _windows(rate_limit_info):
        """{"five_hour": {"utilization": 0.34, "resets_at": ts}, ...} or None."""
        if not isinstance(rate_limit_info, dict):
            return None
        unified = rate_limit_info.get("unifiedWindows")
        if not isinstance(unified, dict):
            return None
        out = {}
        for name, win in unified.items():
            if not isinstance(win, dict):
                continue
            entry = {}
            if isinstance(win.get("utilization"), (int, float)):
                entry["utilization"] = round(float(win["utilization"]), 4)
            if isinstance(win.get("resetsAt"), (int, float)):
                entry["resets_at"] = int(win["resetsAt"])
            if entry:
                out[name] = entry
        return out or None

    def add(self, payload, rate_limit_info=None, label=None):
        """Record one call from the CLI's result envelope. Returns the record."""
        usage = payload.get("usage") if isinstance(payload, dict) else None
        usage = usage if isinstance(usage, dict) else {}
        record = {"label": label, "at": _now_iso()}
        for key in self._COUNTERS:
            value = usage.get(key)
            if key == "thinking_tokens":
                details = usage.get("output_tokens_details")
                value = details.get("thinking_tokens") if isinstance(details, dict) else None
            record[key] = int(value) if isinstance(value, (int, float)) else 0
        cost = payload.get("total_cost_usd") if isinstance(payload, dict) else None
        record["cost_usd"] = round(float(cost), 4) if isinstance(cost, (int, float)) else 0.0
        # Every frame the model opens is a tool turn, and every turn re-sends
        # the whole context (cached, but the meter still counts it). This is
        # why a chunk with 70 frames costs far more than 70 x 790 tokens, and
        # why CLAUDE_CLI_MAX_FRAMES is the first lever.
        turns = payload.get("num_turns") if isinstance(payload, dict) else None
        record["turns"] = int(turns) if isinstance(turns, (int, float)) else 0
        windows = self._windows(rate_limit_info)
        if windows:
            record["windows"] = windows
        with self._lock:
            self.calls.append(record)
            if windows:
                if self.first_windows is None:
                    self.first_windows = windows
                self.last_windows = windows
        return record

    def summary(self):
        with self._lock:
            calls = list(self.calls)
            first, last = self.first_windows, self.last_windows
        totals = {key: sum(c[key] for c in calls) for key in self._COUNTERS}
        totals["cost_usd"] = round(sum(c["cost_usd"] for c in calls), 4)
        totals["turns"] = sum(c.get("turns", 0) for c in calls)
        out = {"calls": len(calls), **totals}
        if first and last:
            for name in sorted(set(first) | set(last)):
                a = first.get(name, {}).get("utilization")
                b = last.get(name, {}).get("utilization")
                entry = {}
                if a is not None:
                    entry["utilization_before"] = a
                if b is not None:
                    entry["utilization_after"] = b
                if a is not None and b is not None:
                    # Negative means the window reset mid-run; keep the raw
                    # numbers rather than clamp them, that is information too.
                    entry["utilization_delta"] = round(b - a, 4)
                reset = last.get(name, {}).get("resets_at")
                if reset is not None:
                    entry["resets_at"] = reset
                    entry["resets_at_iso"] = _iso(reset)
                out.setdefault("windows", {})[name] = entry
        return out

    def describe(self):
        """One line for the log, e.g. '4 calls, 212k in (180k cached), 9k out'."""
        s = self.summary()
        if not s["calls"]:
            return "no claude-cli calls"
        text = (f"{s['calls']} call(s), {_k(s['input_tokens'] + s['cache_read_input_tokens'] + s['cache_creation_input_tokens'])} in "
                f"({_k(s['cache_read_input_tokens'])} cached), {_k(s['output_tokens'])} out, "
                f"{_k(s['thinking_tokens'])} thinking, ~${s['cost_usd']:.2f} at list price")
        five = s.get("windows", {}).get("five_hour")
        if five and "utilization_after" in five:
            text += f"; 5h window at {five['utilization_after'] * 100:.0f}%"
            if "utilization_delta" in five:
                text += f" ({five['utilization_delta'] * 100:+.0f}% this stage)"
            if five.get("resets_at"):
                text += f", resets {_local_clock(five['resets_at'])}"
        return text


USAGE = UsageLedger()


def _now_iso():
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _iso(ts):
    return datetime.fromtimestamp(ts).astimezone().isoformat(timespec="seconds")


def _local_clock(ts):
    return datetime.fromtimestamp(ts).astimezone().strftime("%H:%M %Z")


def _k(n):
    return f"{n / 1000:.1f}k" if n >= 1000 else str(n)


@dataclass
class FrameMeta:
    """A single extracted frame from the recording.

    `number` is the frame's position in the *whole* recording, 1-based, and it
    is the number the model is shown and the number the PDF resolves a
    citation against. It has to be assigned once, from the full manifest, by
    assign_numbers() — see the warning there.
    """
    timestamp_s: float
    kind: str  # "scene_change" or "periodic"
    path: str
    number: int = 0
    # Which video this frame came from when several are summarized as one
    # (pipeline.sh --combine), 1-based; 0 for a single-recording run. The
    # timestamp stays relative to that video, so the part is what keeps two
    # frames at "410.0s" from being the same moment.
    part: int = 0

    @property
    def sort_key(self):
        """Chronological order across videos: by part first, then time."""
        return (self.part, self.timestamp_s)

    def label(self, idx):
        if self.part:
            return (f"[frame {idx} @ video {self.part} {self.timestamp_s:.1f}s "
                    f"({self.kind})]")
        return f"[frame {idx} @ {self.timestamp_s:.1f}s ({self.kind})]"


def assign_numbers(frames):
    """Number every frame by its place in the recording. Returns them sorted.

    This must happen once, over the entire manifest, before chunking — never
    per chunk. _render() below numbers whatever list it is handed, and it is
    called once per chunk, so without a number assigned up front chunk 3's
    fifth frame was announced to the model as "frame 5" and so was chunk 1's.
    The model cited them faithfully; the PDF, which numbers across the whole
    recording, then resolved half the citations to a picture of a completely
    different moment. Found 2026-09-08 in a real lecture summary, where
    "Frame 4" was cited at both 219s and 5484s.
    """
    ordered = sorted(frames, key=lambda f: f.sort_key)
    for index, frame in enumerate(ordered, start=1):
        frame.number = index
    return ordered


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


def _render(frames, transcript, prompt_template, with_paths=False):
    """Sort frames chronologically and fill in the prompt.

    Returns (sorted_frames, user_text). Every backend needs exactly this, and
    the frame order has to match the order the images are attached in.

    `with_paths` appends each frame's absolute path to its manifest line. The
    CLI backend needs that (the model opens the file itself); the SDK backends
    must not have it, because they attach the bytes and a stray filesystem
    path in the prompt only invites the model to talk about paths.
    """
    sorted_frames = sorted(frames, key=lambda f: f.sort_key)
    lines = []
    for i, frame in enumerate(sorted_frames):
        # The frame's own number when it has one, so a chunk announces the
        # numbers the whole recording uses. Falling back to the position in
        # this list is what a caller that never called assign_numbers() gets,
        # and is only correct when the list is the entire manifest.
        line = frame.label(frame.number or (i + 1))
        if with_paths:
            line = f"{line} {Path(frame.path).resolve()}"
        lines.append(line)
    manifest = "\n".join(lines)
    user_text = prompt_template.format(transcript=transcript,
                                       frame_manifest=manifest)
    return sorted_frames, strip_static_markers(user_text)


def strip_static_markers(text):
    """Drop the static-prompt delimiter lines from a rendered prompt.

    They are structure for us, noise for the model. The claude-cli backend has
    already split on them by the time it renders; every other backend renders
    the template whole and simply shouldn't see them.
    """
    if STATIC_PROMPT_BEGIN not in text and STATIC_PROMPT_END not in text:
        return text
    kept = [line for line in text.splitlines()
            if line.strip() not in (STATIC_PROMPT_BEGIN, STATIC_PROMPT_END)]
    return "\n".join(kept)


def split_static_prompt(prompt_template):
    """Split a template into (static_instructions, dynamic_rest).

    A template that marks its unchanging half with STATIC_PROMPT_BEGIN /
    STATIC_PROMPT_END gets that half lifted out; everything else — the chunk
    preamble mapreduce prepends, the reference material summarize.py appends,
    the transcript and the frame manifest — stays in `dynamic_rest` and so
    stays in the piped user turn.

    Returns (None, prompt_template) when the markers are absent or malformed,
    which is what every prompt file older than summarize-v2.md gets: the
    template is then sent exactly as it always was.
    """
    start = prompt_template.find(STATIC_PROMPT_BEGIN)
    end = prompt_template.find(STATIC_PROMPT_END)
    if start < 0 or end < start:
        return None, prompt_template
    static = prompt_template[start + len(STATIC_PROMPT_BEGIN):end].strip()
    if not static:
        return None, prompt_template
    dynamic = (prompt_template[:start]
               + prompt_template[end + len(STATIC_PROMPT_END):])
    return static, dynamic.strip() + "\n\n" 


# ---------------------------------------------------------------------------
# Claude via the Claude Code CLI (subscription auth)
# ---------------------------------------------------------------------------

def _claude_cli_bin():
    """Absolute path to the claude binary, or None.

    ~/.local/bin is checked explicitly because that is where the official
    installer puts it, and a systemd unit or a cron job runs with a PATH that
    usually doesn't include it.
    """
    configured = (os.environ.get("CLAUDE_CLI_BIN") or "").strip()
    if configured:
        return configured if Path(configured).exists() else None
    found = shutil.which("claude")
    if found:
        return found
    fallback = Path.home() / ".local" / "bin" / "claude"
    return str(fallback) if fallback.exists() else None


def _claude_cli_env():
    """The subprocess environment, with API-key auth stripped out.

    See the module docstring: leaving ANTHROPIC_API_KEY set moves the spend
    from the subscription to a metered account without saying so, and leaving
    it set-but-empty fails auth in a way that reads like a broken login.
    """
    env = dict(os.environ)
    for var in _API_KEY_VARS:
        env.pop(var, None)
    # The CLI is not a terminal here; keep its output machine-readable.
    env["CI"] = "1"
    return env


def _claude_cli_cwd():
    """An empty scratch directory to run the CLI in.

    Not the repo root: the CLI auto-discovers CLAUDE.md from its working
    directory, and this project's CLAUDE.md is 38KB of architecture notes that
    have nothing to do with summarizing a lecture. --safe-mode also suppresses
    that, but controlling the cwd doesn't depend on a flag name staying put.
    """
    root = Path(os.environ.get("MEETING_BOT_ROOT", "/opt/meeting-bot"))
    cwd = root / "tmp" / "claude-cli-cwd"
    try:
        cwd.mkdir(parents=True, exist_ok=True)
        return str(cwd)
    except OSError:
        return str(Path.home())


def _cli_timeout():
    try:
        return max(60, int(os.environ.get("CLAUDE_CLI_TIMEOUT_SECONDS",
                                          DEFAULT_CLAUDE_CLI_TIMEOUT)))
    except ValueError:
        return DEFAULT_CLAUDE_CLI_TIMEOUT


def _frame_vision_enabled():
    return (os.environ.get("CLAUDE_CLI_FRAME_VISION", "1").strip().lower()
            not in ("0", "false", "no", "off"))


def _static_prompt_enabled():
    """Whether to hand the unchanging instructions over as a system prompt.

    On by default. Turn it off (CLAUDE_CLI_STATIC_PROMPT=0) for a CLI too old
    to know --append-system-prompt-file or
    --exclude-dynamic-system-prompt-sections; the instructions then travel
    inline in the piped prompt exactly as they used to, and the only thing lost
    is prompt-cache reuse.
    """
    return (os.environ.get("CLAUDE_CLI_STATIC_PROMPT", "1").strip().lower()
            not in ("0", "false", "no", "off"))


def _static_prompt_file(text):
    """Write `text` to a stable, content-addressed file and return its path.

    Content-addressed so the file is byte-identical for byte-identical
    instructions: the same template always lands on the same path with the same
    bytes, and an edited template gets a new one rather than a rewritten one.
    Returns None if it can't be written, in which case the caller falls back to
    sending the instructions inline.
    """
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
    root = Path(os.environ.get("MEETING_BOT_ROOT", "/opt/meeting-bot"))
    path = root / "tmp" / "claude-cli-prompts" / f"{digest}.md"
    try:
        if path.is_file() and path.read_text() == text:
            return path
        path.parent.mkdir(parents=True, exist_ok=True)
        # Write-then-rename: several chunks summarize in parallel and would
        # otherwise race on a half-written file.
        tmp = path.with_suffix(f".{os.getpid()}.tmp")
        tmp.write_text(text)
        os.replace(tmp, path)
        return path
    except OSError as exc:
        print(f"  warning: could not cache the static prompt ({exc}) — "
              f"sending it inline", file=sys.stderr)
        return None


def _frame_max_dimension():
    """Long edge, in pixels, for the frame copies the CLI is pointed at."""
    raw = os.environ.get("FRAME_MAX_DIMENSION", DEFAULT_FRAME_MAX_DIMENSION)
    try:
        value = int(raw)
    except (TypeError, ValueError):
        print(f"  warning: FRAME_MAX_DIMENSION={raw!r} is not a number — "
              f"using {DEFAULT_FRAME_MAX_DIMENSION}", file=sys.stderr)
        return DEFAULT_FRAME_MAX_DIMENSION
    return max(0, value)


def _int_env(name, default, floor=0):
    raw = os.environ.get(name, default)
    try:
        value = int(float(raw))
    except (TypeError, ValueError):
        print(f"  warning: {name}={raw!r} is not a number — using {default}",
              file=sys.stderr)
        return default
    return max(floor, value)


def _max_frames():
    return _int_env("CLAUDE_CLI_MAX_FRAMES", DEFAULT_MAX_FRAMES)


def _max_wait_seconds():
    return _int_env("CLAUDE_CLI_MAX_WAIT_SECONDS", DEFAULT_MAX_WAIT_SECONDS)


def _rate_limit_poll_seconds():
    return _int_env("CLAUDE_CLI_RATE_LIMIT_POLL_SECONDS",
                    DEFAULT_RATE_LIMIT_POLL_SECONDS, floor=5)


def thin_frames(frames, cap):
    """At most `cap` frames, chosen to still cover the whole window.

    Scene changes are kept first — there are rarely more than a handful, and
    each one is a slide transition the notes should cite — and the periodic
    frames fill the rest at an even stride, so a 36-minute chunk capped at 20
    still shows the model something every couple of minutes rather than the
    first twenty minutes in detail and nothing after.

    The frames' numbers are untouched: they were assigned over the whole
    manifest by assign_numbers(), and the ones left out keep theirs, so a
    citation still resolves to the right picture in the PDF. Only what the
    model is *offered* shrinks. cap <= 0 returns the list as it was.
    """
    if cap <= 0 or len(frames) <= cap:
        return list(frames)
    ordered = sorted(frames, key=lambda f: f.sort_key)
    scene = [f for f in ordered if f.kind == "scene_change"]
    periodic = [f for f in ordered if f.kind != "scene_change"]
    if len(scene) >= cap:
        # Even the scene changes alone are over budget: stride through them.
        kept = _evenly(scene, cap)
    else:
        kept = scene + _evenly(periodic, cap - len(scene))
    return sorted(kept, key=lambda f: f.sort_key)


def _evenly(items, n):
    """n items from `items` at an even stride, first and last included."""
    if n <= 0 or not items:
        return []
    if n >= len(items):
        return list(items)
    if n == 1:
        return [items[len(items) // 2]]
    step = (len(items) - 1) / (n - 1)
    picked = []
    seen = set()
    for i in range(n):
        idx = round(i * step)
        if idx not in seen:
            seen.add(idx)
            picked.append(items[idx])
    return picked


_warned_no_pillow_downscale = False


def _downscale_one(src, max_dim):
    """Return a path to a copy of `src` whose long edge is <= max_dim.

    Returns `src` itself when the image is already small enough, when Pillow
    isn't installed, or when anything at all goes wrong — a slightly expensive
    frame beats a missing one.
    """
    global _warned_no_pillow_downscale
    try:
        from PIL import Image
    except ImportError:
        if not _warned_no_pillow_downscale:
            _warned_no_pillow_downscale = True
            print("  warning: Pillow is not installed — frames are sent at "
                  "full resolution", file=sys.stderr)
        return src

    source = Path(src)
    dest = source.parent / LLM_FRAME_SUBDIR.format(max_dim=max_dim) / source.name
    try:
        if dest.is_file() and dest.stat().st_mtime >= source.stat().st_mtime:
            return str(dest)
        with Image.open(source) as img:
            if max(img.size) <= max_dim:
                return str(source)
            img = img.convert("RGB")
            img.thumbnail((max_dim, max_dim), Image.LANCZOS)
            dest.parent.mkdir(parents=True, exist_ok=True)
            tmp = dest.with_suffix(f".{os.getpid()}.tmp")
            img.save(tmp, "JPEG", quality=85, optimize=True)
        os.replace(tmp, dest)
        return str(dest)
    except (OSError, ValueError) as exc:
        print(f"  warning: could not downscale {source.name} ({exc}) — "
              f"sending it at full resolution", file=sys.stderr)
        return str(source)


def _downscale_frames(frames):
    """Frame copies pointed at downscaled images, for the CLI to Read.

    The originals are left exactly where they are: pdf.py crops and embeds
    them, so it needs the full-resolution files. Only the paths the model is
    shown change, and only for this backend.
    """
    max_dim = _frame_max_dimension()
    if max_dim <= 0 or not frames:
        return frames, 0
    resized = 0
    out = []
    for frame in frames:
        path = _downscale_one(frame.path, max_dim)
        if path != frame.path:
            resized += 1
            out.append(replace(frame, path=path))
        else:
            out.append(frame)
    return out, resized


_VISION_PREAMBLE = """\
The frame manifest below lists keyframes extracted from the recording, each
with its timestamp and the absolute path to the image on this machine. Use the
Read tool to open the frames you need before writing the summary — cite a frame
only after you have looked at it. Read tool access is the only tool you have;
do not attempt anything else.

"""


def _looks_unauthenticated(text):
    lowered = (text or "").lower()
    return any(marker in lowered for marker in _NOT_LOGGED_IN_MARKERS)


def parse_cli_output(stdout):
    """(result_envelope, last_rate_limit_info) from what the CLI printed.

    `--output-format stream-json` prints one JSON object per line: system
    messages, the assistant turns, `rate_limit_event`s, and finally the same
    `result` envelope that `--output-format json` prints alone. Both shapes
    are accepted — a whole-output parse first (the plain envelope, which is
    also what the unit tests and an older stub feed in), then line by line.
    Returns (None, None) when there is no envelope at all, which the caller
    reads as "the CLI refused before it got that far".
    """
    text = (stdout or "").strip()
    if not text:
        return None, None
    try:
        whole = json.loads(text)
        if isinstance(whole, dict):
            return whole, None
    except json.JSONDecodeError:
        pass
    envelope = None
    rate_limit = None
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(obj, dict):
            continue
        kind = obj.get("type")
        if kind == "result":
            envelope = obj
        elif kind == "rate_limit_event":
            info = obj.get("rate_limit_info")
            if isinstance(info, dict):
                rate_limit = info
    return envelope, rate_limit


def _rate_limit_from(payload, rate_limit_info, stderr=""):
    """A ClaudeCliRateLimited if this call hit the usage window, else None.

    Three signals, most reliable first: the `rate_limit_event` saying
    `rejected` (with the reset time and which window), `api_error_status`
    429 on the envelope, and finally the wording of the result — the last
    covers a CLI old enough to print "usage limit reached|<epoch>" and no
    event at all.
    """
    result = payload.get("result") if isinstance(payload, dict) else ""
    result = result if isinstance(result, str) else ""
    is_error = bool(payload.get("is_error")) or payload.get("subtype") == "error"

    if isinstance(rate_limit_info, dict) and rate_limit_info.get("status") == "rejected":
        window = rate_limit_info.get("rateLimitType") or "unknown"
        resets_at = rate_limit_info.get("resetsAt")
        if not isinstance(resets_at, (int, float)):
            windows = rate_limit_info.get("unifiedWindows") or {}
            candidate = windows.get(window) if isinstance(windows, dict) else None
            resets_at = candidate.get("resetsAt") if isinstance(candidate, dict) else None
        resets_at = int(resets_at) if isinstance(resets_at, (int, float)) else None
        return ClaudeCliRateLimited(
            f"claude usage window exhausted ({window}"
            f"{', resets ' + _iso(resets_at) if resets_at else ''}): "
            f"{result or stderr or 'rate_limit_event rejected'}",
            resets_at=resets_at, window=window)

    if not is_error:
        return None
    text = result or stderr or ""
    if payload.get("api_error_status") == 429 or _RATE_LIMIT_RE.search(text):
        m = _RATE_LIMIT_RESET_RE.search(text)
        resets_at = int(m.group(1)) if m else None
        return ClaudeCliRateLimited(
            f"claude usage window exhausted"
            f"{' (resets ' + _iso(resets_at) + ')' if resets_at else ''}: "
            f"{text or 'HTTP 429'}",
            resets_at=resets_at, window=None)
    return None


def _describe_call(record):
    """The per-call usage line, e.g. '61.2k in (48.0k cached), 4.1k out'."""
    text = (f"{_k(record['input_tokens'] + record['cache_read_input_tokens'] + record['cache_creation_input_tokens'])} in "
            f"({_k(record['cache_read_input_tokens'])} cached), "
            f"{_k(record['output_tokens'])} out, {_k(record['thinking_tokens'])} thinking, "
            f"{record.get('turns', 0)} turn(s), ~${record['cost_usd']:.2f}")
    five = (record.get("windows") or {}).get("five_hour")
    if five and "utilization" in five:
        text += f"; 5h window {five['utilization'] * 100:.0f}%"
        if five.get("resets_at"):
            text += f", resets {_local_clock(five['resets_at'])}"
    return text


def _run_claude_cli(argv, prompt, env, cwd, timeout, label="claude-cli"):
    """One `claude -p` invocation. Raises on anything that isn't a summary.

    Split out from summarize_claude_cli so with_retries wraps exactly the
    fallible part, and so the unit tests can drive it against a stub binary.
    """
    try:
        proc = subprocess.run(argv, input=prompt, env=env, cwd=cwd,
                              capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        # "timed out" is in retry.py's retryable wording, so a CLI that hangs
        # once gets another attempt rather than failing the stage.
        raise ClaudeCliError(
            f"claude CLI timed out after {timeout}s") from exc
    except OSError as exc:
        raise BackendUnavailable(f"could not run the claude CLI: {exc}") from exc

    stdout = (proc.stdout or "").strip()
    stderr = (proc.stderr or "").strip()

    # The CLI exits 0 even when it could not authenticate, so the exit code is
    # never the whole story — the JSON body is.
    if not stdout:
        detail = stderr or f"no output (exit {proc.returncode})"
        if _looks_unauthenticated(detail):
            raise BackendUnavailable(f"claude CLI is not logged in: {detail}")
        raise ClaudeCliError(f"claude CLI produced no output: {detail}")

    payload, rate_limit_info = parse_cli_output(stdout)
    if payload is None:
        # A JSON output format should always give us a result envelope.
        # Anything else is the CLI refusing before it got that far.
        if _looks_unauthenticated(stdout):
            raise BackendUnavailable(f"claude CLI is not logged in: {stdout}")
        raise ClaudeCliError(
            f"claude CLI returned non-JSON output: {stdout[:400]}")

    # Whatever happens next, the tokens were spent — and on a rejected call
    # the event still carries the meter, which is the number worth keeping.
    record = USAGE.add(payload, rate_limit_info, label=label)

    # Before the auth check: the CLI's own error classifier files "usage
    # limit reached" beside "not logged in", and an exhausted window read as
    # a signed-out CLI would hand the summary to Gemini instead of waiting.
    limited = _rate_limit_from(payload, rate_limit_info, stderr)
    if limited is not None:
        raise limited

    result = payload.get("result")
    if not isinstance(result, str):
        result = ""

    if payload.get("is_error") or payload.get("subtype") == "error":
        detail = result or payload.get("error") or stderr or "unknown error"
        if _looks_unauthenticated(detail):
            raise BackendUnavailable(
                f"claude CLI is not logged in: {detail}\n"
                f"  Run `claude auth login` (or `claude setup-token` for an "
                f"unattended box) as the user this pipeline runs as.")
        raise ClaudeCliError(f"claude CLI failed: {detail}")

    denials = payload.get("permission_denials") or []
    if denials:
        # Not fatal: the summary still exists, but it was written without the
        # frames the model asked for, so say so rather than shipping it as if
        # the pictures had been seen.
        print(f"  warning: the CLI was denied {len(denials)} tool call(s) — "
              f"frames may not have been read", file=sys.stderr)

    if not result.strip():
        raise ClaudeCliError("claude CLI returned an empty summary")
    print(f"     {label}: {_describe_call(record)}")
    return result.strip()


def _wait_for_window(exc, label, waited_so_far):
    """Sleep out a hit usage window. Returns the seconds slept.

    Raises the exception back when the wait would exceed
    CLAUDE_CLI_MAX_WAIT_SECONDS in total for this call — the weekly limit,
    or a window that keeps refusing after its reset — so the stage fails
    with the reset time recorded and a later `--resume-all` picks it up.
    """
    max_wait = _max_wait_seconds()
    now = time.time()
    if exc.resets_at:
        delay = exc.resets_at - now + RATE_LIMIT_MARGIN_SECONDS
        # A reset time in the past means the CLI's clock and ours disagree,
        # or the window already turned over while we were reading the error:
        # try again soon rather than never.
        delay = max(delay, RATE_LIMIT_MARGIN_SECONDS)
        why = f"resets {_local_clock(exc.resets_at)}"
    else:
        delay = _rate_limit_poll_seconds()
        why = "no reset time reported"
    if max_wait <= 0 or waited_so_far + delay > max_wait:
        raise exc
    until = now + delay
    print(f"     {label}: usage window exhausted ({exc.window or 'window'}, {why}) — "
          f"waiting {delay / 60:.0f} min, until {_local_clock(until)}",
          file=sys.stderr)
    if WAIT_HOOK is not None:
        try:
            WAIT_HOOK(waiting_until=int(until), resets_at=exc.resets_at,
                      window=exc.window)
        except Exception as hook_exc:  # noqa: BLE001 — bookkeeping only
            print(f"     warning: could not record the wait: {hook_exc}",
                  file=sys.stderr)
    _sleep(delay)
    if WAIT_HOOK is not None:
        try:
            WAIT_HOOK(waiting_until=None, resets_at=None, window=None)
        except Exception:  # noqa: BLE001
            pass
    print(f"     {label}: window should have reset — trying again", file=sys.stderr)
    return delay


def summarize_claude_cli(frames: List[FrameMeta], transcript: str,
                         prompt_template: str) -> str:
    """Claude Code CLI in print mode — the default backend.

    Spends the operator's Claude subscription rather than a metered API key.
    """
    binary = _claude_cli_bin()
    if binary is None:
        raise BackendUnavailable(
            "the claude CLI was not found.\n"
            "  Install it with:  curl -fsSL https://claude.ai/install.sh | bash\n"
            "  then log in once: claude auth login\n"
            "  If it lives somewhere unusual, set CLAUDE_CLI_BIN to its path.")

    model = (os.environ.get("CLAUDE_CLI_MODEL")
             or os.environ.get("ANTHROPIC_MODEL")
             or DEFAULT_CLAUDE_CLI_MODEL)
    effort = effort_level()
    vision = _frame_vision_enabled() and bool(frames)

    # Fewer frames offered, when capped: they are the bulk of a call's input
    # against the subscription window. The numbers stay global, so the ones
    # left out still resolve in the PDF if the model never cites them.
    offered = thin_frames(frames, _max_frames())
    thinned = len(frames) - len(offered)

    # Downscaled copies, so a 1920x1080 keyframe doesn't cost ~1,844 tokens
    # every time the model opens it. Only the paths the CLI is given change —
    # pdf.py still crops and embeds the full-resolution originals.
    resized = 0
    llm_frames = offered
    if vision:
        llm_frames, resized = _downscale_frames(offered)

    # The half of the template that never varies goes to the CLI as a system
    # prompt read from a stable file, so the prefix is byte-identical across
    # runs and across the chunks of one run. Everything that does vary — the
    # chunk label, the reference material, the transcript, the frame paths —
    # stays in the piped user turn. A template with no markers splits to
    # (None, itself) and behaves exactly as it did before.
    static_prompt = None
    if _static_prompt_enabled():
        static_prompt, prompt_template = split_static_prompt(prompt_template)

    sorted_frames, user_text = _render(llm_frames, transcript, prompt_template,
                                       with_paths=vision)
    if vision:
        user_text = _VISION_PREAMBLE + user_text

    static_prompt_path = None
    if static_prompt:
        static_prompt_path = _static_prompt_file(static_prompt)
        if static_prompt_path is None:
            # Couldn't cache it; put it back where it has always been rather
            # than summarizing without any instructions at all.
            user_text = static_prompt + "\n\n" + user_text

    argv = [
        binary, "-p",
        # stream-json, not json: the same result envelope arrives as the last
        # line, and beside it the CLI emits a rate_limit_event carrying the
        # subscription's own 5-hour / 7-day meters and, when the window is
        # exhausted, the reset time. --verbose is what print mode requires
        # for the streaming format.
        "--output-format", "stream-json", "--verbose",
        "--model", model,
        "--effort", effort,
        # Deterministic context: no CLAUDE.md, no hooks, no plugins, no MCP
        # servers, no custom agents. Auth and the built-in tools still work.
        "--safe-mode",
        # Every run would otherwise leave a full transcript in ~/.claude —
        # this box summarizes hour-long lectures on a 15GB disk.
        "--no-session-persistence",
    ]

    if static_prompt_path is not None:
        argv += ["--append-system-prompt-file", str(static_prompt_path),
                 # Moves cwd / env info / date / git status out of the system
                 # prompt and into the first user message. Without it the
                 # built-in prompt sits in front of ours and changes daily,
                 # which puts a moving target ahead of everything we just made
                 # stable.
                 "--exclude-dynamic-system-prompt-sections"]

    if vision:
        # Read only, and only inside the frame directories: both the
        # originals' and whatever directory the downscaled copies landed in.
        # The copies live in a subdirectory of the originals', and the frames
        # of one run share a directory, so this is normally one entry.
        frame_dirs = sorted({str(Path(f.path).resolve().parent)
                             for f in list(frames) + list(sorted_frames)})
        argv += ["--tools", "Read", "--allowedTools", "Read"]
        for directory in frame_dirs:
            argv += ["--add-dir", directory]
    else:
        # No tools at all: the model has nothing to open and nothing to touch.
        argv += ["--tools", ""]

    label = f"claude-cli/{model} (effort={effort})"
    detail = "vision=on" if vision else "vision=off"
    if thinned:
        detail += f", {thinned} of {len(frames)} left out (CLAUDE_CLI_MAX_FRAMES)"
    if resized:
        detail += f", {resized} downscaled to {_frame_max_dimension()}px"
    if static_prompt_path is not None:
        detail += ", cacheable system prompt"
    print(f"     {label}: {len(sorted_frames)} frame(s), {detail}")

    # with_retries handles a busy server; this loop handles an exhausted
    # subscription window, which is neither transient nor a reason to hand
    # the summary to another provider. ClaudeCliRateLimited is not retryable,
    # so it comes straight back out of with_retries, and the wait is bounded
    # in total by CLAUDE_CLI_MAX_WAIT_SECONDS for this one call.
    waited = 0.0
    while True:
        try:
            # The trailing positional is _run_claude_cli's own label; the
            # keyword one is with_retries', which it keeps for its log line.
            text = with_retries(
                _run_claude_cli, argv, user_text, _claude_cli_env(),
                _claude_cli_cwd(), _cli_timeout(), label, label=label,
            )
            break
        except ClaudeCliRateLimited as exc:
            waited += _wait_for_window(exc, label, waited)

    _record_used("claude-cli", model)
    return text


# ---------------------------------------------------------------------------
# Google Gemini
# ---------------------------------------------------------------------------

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


# chain-name -> backend function.
#
# `anthropic`, `claude` and `fcc` are kept as aliases of the CLI backend so an
# existing .env whose chain reads `anthropic,gemini` keeps working. There is no
# separate API-key backend behind those names any more — the subscription is
# the only Anthropic path this project has.
_BACKENDS = {
    "claude-cli": summarize_claude_cli,
    "claude_cli": summarize_claude_cli,
    "claude": summarize_claude_cli,
    "cli": summarize_claude_cli,
    "anthropic": summarize_claude_cli,
    "fcc": summarize_claude_cli,   # alias kept for the user's older configs
    "gemini": summarize_gemini,
}


def summarize_with_fallback(frames: List[FrameMeta], transcript: str,
                            prompt_template: str) -> str:
    """Walk SUMMARY_FALLBACK_CHAIN in order; first backend to return wins.

    Each backend has already retried its own transient failures by the time it
    raises here, so reaching the next entry means that provider is genuinely
    unusable right now — not merely busy.

    Chain syntax: comma-separated names, e.g. "claude-cli,gemini". Unknown
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
        except ClaudeCliRateLimited as exc:
            # The subscription window is exhausted and the in-process wait
            # gave up. Not a reason to spend a second provider: the operator
            # chose to wait for the subscription, so this fails the stage
            # (resumable, reset time recorded) instead of advancing.
            print(f"  !! {name}: {exc}\n"
                  f"     not falling through to the next backend — the run "
                  f"resumes on Claude once the window resets", file=sys.stderr)
            raise
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
            f"(expected claude-cli, gemini, or fallback)"
        )
    return func(frames, transcript, prompt_template)
