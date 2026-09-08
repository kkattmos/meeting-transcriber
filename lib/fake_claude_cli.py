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

It answers in the CLI's `--output-format json` envelope, because that is what
llm_client parses: `result` holds the text, `is_error` decides whether the
backend failed, and `permission_denials` drives the "frames may not have been
read" warning.

Modes (--mode, or $FAKE_CLAUDE_MODE):
  ok             the canned summary                          (default)
  not-logged-in  the exact envelope a signed-out CLI returns: is_error true,
                 exit status 0. Verifies the chain treats it as
                 BackendUnavailable and falls through to Gemini rather than
                 retrying something no retry can fix.
  overloaded     a retryable failure, for the backoff path
  denied         a summary plus a permission_denials entry
  empty          is_error false with an empty result
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


def _envelope(result, is_error=False, denials=(), reason=None):
    """The shape `claude -p --output-format json` prints.

    Only the fields llm_client reads are meaningful; the rest are present so
    the stub's output stays a realistic sample of the real thing.
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
        "total_cost_usd": 0,
        "permission_denials": list(denials),
        "terminal_reason": reason or ("api_error" if is_error else "stop"),
        "usage": {"input_tokens": 1000, "output_tokens": 500},
    }


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
    if mode == "not-logged-in":
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

    json.dump(out, sys.stdout)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
