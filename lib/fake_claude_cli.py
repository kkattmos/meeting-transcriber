#!/usr/bin/env python3
"""
A stand-in for the `claude` binary, for lib/test_media_e2e.sh.

The summarize stage no longer speaks HTTP to Anthropic — it runs `claude -p`
and spends the operator's subscription. So the seam that used to be a stub
Messages-API server (lib/fake_api_server.py's AnthropicHandler) is a stub
*executable* instead: point CLAUDE_CLI_BIN at this file and the real
summarize/llm_client.py runs against it unmodified.

What it records (one JSON line appended to --record, or $FAKE_CLAUDE_RECORD):

    {"api": "claude-cli",
     "argv":   [...],          the exact command line llm_client built
     "prompt": "...",          everything it sent on stdin
     "env": {"ANTHROPIC_API_KEY": null, ...}}   the auth vars it did NOT scrub

`env` is the point of the whole file. A summarize run that leaves
ANTHROPIC_API_KEY in the child's environment still produces a perfectly good
summary — billed to a metered API account instead of the subscription the
operator meant to spend. Nothing about the output reveals it, so the only place
that can catch the regression is a stub that reports what it was handed.

It answers the way the real CLI does for the output format it was asked
for. With `--output-format stream-json` (what llm_client passes) that is one
JSON object per line: a `system` init line, a `rate_limit_event` carrying the
subscription's 5-hour / 7-day meters, and the `result` envelope last. With
`json` it is the envelope alone. llm_client parses `result` for the text,
`is_error` for failure, `permission_denials` for the "frames may not have
been read" warning, `usage` for the token ledger, and the rate_limit_event
for the meter and for an exhausted window.

Modes (--mode, or $FAKE_CLAUDE_MODE):
  ok             the canned summary                          (default)
  not-logged-in  the exact envelope a signed-out CLI returns: is_error true,
                 exit status 0. Verifies the chain treats it as
                 BackendUnavailable and falls through to Gemini rather than
                 retrying something no retry can fix.
  overloaded     a retryable failure, for the backoff path
  denied         a summary plus a permission_denials entry
  empty          is_error false with an empty result
  rate-limited   the usage window is exhausted: a rate_limit_event with
                 status "rejected" and a reset time $FAKE_CLAUDE_RESET_IN
                 seconds from now (default 2), and an is_error envelope
                 with api_error_status 429. Exit status 0, like the real one.
  rate-limited-once  the same, but only for the first call recorded in
                 $FAKE_CLAUDE_STATE (a counter file); later calls succeed.
                 This is the wait-then-retry path end to end.
"""
import argparse
import json
import os
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

try:
    from fake_api_server import SUMMARY_TEXT
except ImportError:  # running the stub from somewhere odd
    SUMMARY_TEXT = "Stub summary *(Frame 1 @ 2.0s)*.\n"

# The vars llm_client._claude_cli_env() promises to strip. Recorded as null
# when absent, so the test asserts on presence rather than on a missing key.
_AUTH_VARS = ("ANTHROPIC_API_KEY", "ANTHROPIC_API_KEY_1",
              "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL")


def _envelope(result, is_error=False, denials=(), reason=None,
              api_error_status=None):
    """The shape `claude -p --output-format json` prints.

    Only the fields llm_client reads are meaningful; the rest are present so
    the stub's output stays a realistic sample of the real thing. The usage
    numbers are fixed so the media test can assert on the ledger's totals.
    """
    return {
        "type": "result",
        "subtype": "error" if is_error else "success",
        "is_error": is_error,
        "result": result,
        "session_id": str(uuid.uuid4()),
        "duration_ms": 12,
        "duration_api_ms": 8,
        "num_turns": 1,
        "total_cost_usd": 0.25,
        "api_error_status": api_error_status,
        "permission_denials": list(denials),
        "terminal_reason": reason or ("api_error" if is_error else "stop"),
        "usage": {"input_tokens": 1000, "output_tokens": 500,
                  "cache_read_input_tokens": 4000,
                  "cache_creation_input_tokens": 0,
                  "output_tokens_details": {"thinking_tokens": 120}},
    }


def _rate_limit_event(status, resets_at, utilization=0.42):
    """The `rate_limit_event` line stream-json carries beside the result."""
    info = {
        "status": status,
        "rateLimitType": "five_hour",
        "unifiedWindows": {
            "five_hour": {"utilization": utilization, "resetsAt": resets_at},
            "seven_day": {"utilization": 0.11, "resetsAt": resets_at + 6 * 86400},
        },
    }
    if status == "rejected":
        info["resetsAt"] = resets_at
    return {"type": "rate_limit_event", "rate_limit_info": info,
            "session_id": "stub", "uuid": str(uuid.uuid4())}


def _bump_counter(path):
    """Calls so far, counted in a file; 1 for the first call."""
    try:
        count = int(Path(path).read_text().strip() or 0)
    except (OSError, ValueError):
        count = 0
    count += 1
    Path(path).write_text(str(count))
    return count


def main(argv):
    ap = argparse.ArgumentParser(add_help=False)
    ap.add_argument("--record", default=os.environ.get("FAKE_CLAUDE_RECORD"))
    ap.add_argument("--mode", default=os.environ.get("FAKE_CLAUDE_MODE", "ok"))
    # Everything else is the command line under test — captured, not parsed.
    known, passthrough = ap.parse_known_args(argv[1:])

    prompt = ""
    if not sys.stdin.isatty():
        try:
            prompt = sys.stdin.read()
        except (OSError, UnicodeDecodeError):
            prompt = ""

    if known.record:
        entry = {
            "api": "claude-cli",
            "argv": passthrough,
            "prompt": prompt,
            "cwd": os.getcwd(),
            "env": {var: os.environ.get(var) for var in _AUTH_VARS},
        }
        with open(known.record, "a") as fh:
            fh.write(json.dumps(entry) + "\n")

    mode = (known.mode or "ok").strip().lower()
    if mode == "rate-limited-once":
        state = os.environ.get("FAKE_CLAUDE_STATE") or (
            (known.record or "/tmp/fake_claude") + ".calls")
        mode = "rate-limited" if _bump_counter(state) == 1 else "ok"

    import time
    resets_at = int(time.time()) + int(os.environ.get("FAKE_CLAUDE_RESET_IN", "2"))
    event = _rate_limit_event("allowed", resets_at)

    if mode == "rate-limited":
        # What a real CLI prints when the 5-hour window is spent: the event
        # says rejected and names the reset; the envelope is an API error
        # with status 429. Exit status 0, as ever.
        event = _rate_limit_event("rejected", resets_at, utilization=1.0)
        out = _envelope("API Error: 429 You've hit your limit \u00b7 resets soon",
                        is_error=True, api_error_status=429)
    elif mode == "not-logged-in":
        # Verbatim from a signed-out claude 2.1.x, exit code included: the CLI
        # exits 0 here, which is why llm_client parses the body instead of
        # trusting the status.
        out = _envelope("Not logged in · Please run /login", is_error=True)
    elif mode == "overloaded":
        out = _envelope("API Error: 503 upstream is overloaded", is_error=True)
    elif mode == "denied":
        out = _envelope(SUMMARY_TEXT, denials=[
            {"tool_name": "Read", "tool_use_id": "toolu_stub"},
        ])
    elif mode == "empty":
        out = _envelope("")
    else:
        out = _envelope(SUMMARY_TEXT)

    if "stream-json" in passthrough:
        # One object per line, the result last — the same order the CLI
        # uses. The init line is here so the parser has to skip something.
        for obj in ({"type": "system", "subtype": "init", "session_id": "stub"},
                    event, out):
            sys.stdout.write(json.dumps(obj) + "\n")
        return 0
    json.dump(out, sys.stdout)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
