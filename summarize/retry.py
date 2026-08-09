#!/usr/bin/env python3
"""
Retry policy shared by every summarize backend.

The problem this solves: a hosted LLM answering 503 "server is busy" (or 429,
or a dropped connection) used to fail the whole summarize stage, and on the
fallback chain it burned a perfectly healthy provider that happened to be busy
for two seconds. Both are transient. Retrying the same backend a few times with
backoff fixes far more runs than moving on immediately does.

Policy:
  * Retryable: HTTP 408, 409, 425, 429, 500, 502, 503, 504, plus connection /
    timeout errors, plus provider-specific "overloaded"/"UNAVAILABLE"/"busy"
    wording that some SDKs raise without a usable status code.
  * NOT retryable: 400, 401, 403, 404, 422 — a bad key or a malformed request
    fails the same way forever, and retrying just delays the fallback to a
    backend that would have worked.
  * Backoff: exponential from SUMMARY_RETRY_BASE_SECONDS, doubling, with full
    jitter, capped at SUMMARY_RETRY_MAX_SECONDS. Jitter matters when several
    chunks are summarized in parallel — without it they all retry in lockstep
    and hit the busy server simultaneously.
  * `Retry-After` from the response wins over the computed backoff when present
    and sane; the server knows better than we do.

Env vars:
  SUMMARY_MAX_RETRIES         default 5  (total attempts, not extra attempts)
  SUMMARY_RETRY_BASE_SECONDS  default 2.0
  SUMMARY_RETRY_MAX_SECONDS   default 60.0
"""
import os
import random
import re
import sys
import time

RETRYABLE_STATUS = {408, 409, 425, 429, 500, 502, 503, 504}
NON_RETRYABLE_STATUS = {400, 401, 403, 404, 405, 422}

# Wording used by providers that raise without a machine-readable status.
# Gemini says UNAVAILABLE/RESOURCE_EXHAUSTED, Anthropic says "overloaded_error",
# NIM and assorted proxies just say "server is busy".
_RETRYABLE_PATTERNS = re.compile(
    r"(overload|unavailable|resource[_ ]exhausted|server is busy|try again|"
    r"temporarily|timeout|timed out|connection reset|connection aborted|"
    r"broken pipe|too many requests|rate.?limit|capacity|502|503|504|529)",
    re.IGNORECASE,
)


def _status_of(exc):
    """Best-effort HTTP status from an SDK exception.

    Every SDK spells this differently: the OpenAI and Anthropic clients use
    `status_code`, some wrap it in `.response.status_code`, urllib uses `.code`.
    Falls back to scraping a 3-digit code out of the message.
    """
    for attr in ("status_code", "code", "http_status"):
        value = getattr(exc, attr, None)
        if isinstance(value, int):
            return value
    response = getattr(exc, "response", None)
    if response is not None:
        value = getattr(response, "status_code", None)
        if isinstance(value, int):
            return value
    m = re.search(r"\b(4\d\d|5\d\d)\b", str(exc))
    if m:
        return int(m.group(1))
    return None


def _retry_after(exc):
    """Seconds requested by the server via the Retry-After header, if any."""
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None) if response is not None else None
    if not headers:
        return None
    for key in ("retry-after", "Retry-After", "x-ratelimit-reset-after"):
        try:
            value = headers.get(key)
        except AttributeError:
            return None
        if value is None:
            continue
        try:
            seconds = float(value)
        except (TypeError, ValueError):
            continue
        # A server asking us to wait an hour is telling us to give up and let
        # the fallback chain move on, not to sleep through the whole run.
        if 0 <= seconds <= 300:
            return seconds
    return None


def is_retryable(exc):
    """True when retrying the same backend could plausibly succeed."""
    status = _status_of(exc)
    if status in NON_RETRYABLE_STATUS:
        return False
    if status in RETRYABLE_STATUS:
        return True

    # Network-layer failures never carry a status and are always worth a retry.
    if isinstance(exc, (ConnectionError, TimeoutError, OSError)):
        return True

    name = type(exc).__name__.lower()
    if any(k in name for k in ("timeout", "connection", "overload", "unavailable",
                               "ratelimit", "serviceunavailable", "internalserver")):
        return True

    return bool(_RETRYABLE_PATTERNS.search(str(exc)))


def backoff_seconds(attempt, base=None, cap=None, jitter=True):
    """Delay before attempt N (1-based). Exponential with full jitter."""
    base = float(os.environ.get("SUMMARY_RETRY_BASE_SECONDS", 2.0)) if base is None else base
    cap = float(os.environ.get("SUMMARY_RETRY_MAX_SECONDS", 60.0)) if cap is None else cap
    raw = min(cap, base * (2 ** max(0, attempt - 1)))
    if not jitter:
        return raw
    # Full jitter (random between 0 and raw) rather than raw±10%: parallel
    # chunk requests must not retry in lockstep against a busy server.
    return random.uniform(0, raw)


def max_attempts():
    try:
        return max(1, int(os.environ.get("SUMMARY_MAX_RETRIES", 5)))
    except ValueError:
        return 5


def with_retries(func, *args, label="request", sleep=time.sleep, **kwargs):
    """Call func(*args, **kwargs), retrying transient failures with backoff.

    Re-raises the last exception once attempts run out, so the caller (the
    fallback chain) can move on to the next backend.
    """
    attempts = max_attempts()
    last = None
    for attempt in range(1, attempts + 1):
        try:
            return func(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001 — classified by is_retryable
            last = exc
            if not is_retryable(exc):
                raise
            if attempt >= attempts:
                break
            delay = _retry_after(exc)
            source = "Retry-After" if delay is not None else "backoff"
            if delay is None:
                delay = backoff_seconds(attempt)
            status = _status_of(exc)
            status_str = f"HTTP {status}" if status else type(exc).__name__
            print(
                f"     {label}: {status_str} — retrying in {delay:.1f}s "
                f"({source}, attempt {attempt}/{attempts})",
                file=sys.stderr,
            )
            sleep(delay)
    raise last
