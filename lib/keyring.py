#!/usr/bin/env python3
"""
Round-robin API-key rotation, backed by numbered environment variables.

Three providers in this project ship more than one key so quota is spread
across accounts: Gemini (up to 3), AssemblyAI (up to 3) and
youtube-transcript.io (up to 10). They all read their keys the same way:

    GEMINI_API_KEY_1=...        ASSEMBLYAI_API_KEY_1=...   YT_TRANSCRIPT_KEY_1=...
    GEMINI_API_KEY_2=...        ASSEMBLYAI_API_KEY_2=...   YT_TRANSCRIPT_KEY_2=...
    GEMINI_API_KEY_3=...        ASSEMBLYAI_API_KEY_3=...   ... up to _10

The unnumbered name (`GEMINI_API_KEY`) is accepted as slot 1, so a single-key
setup — and every pre-existing .env — keeps working untouched.

WHY A CURSOR FILE. Rotation only spreads quota if consecutive *processes* start
at different keys; a per-process cursor would send every run at key #1 and
exhaust that account first. The cursor therefore lives on disk, under
$MEETING_BOT_ROOT/state/keycursor.json, and advances past the key that just
worked. It is written under an flock so two concurrent runs can't clobber each
other's advance, and every failure to read or write it is non-fatal: a lost
cursor costs one duplicated request, never a failed transcription.

Usage:

    ring = KeyRing.from_env("GEMINI_API_KEY", aliases=("GOOGLE_API_KEY",),
                            max_slots=3)
    for slot, key in ring.rotate():
        try:
            result = call_api(key)
        except Retryable:
            continue
        ring.commit(slot)      # next process starts after this key
        return result
"""
import json
import os
import sys
from pathlib import Path

# Slot caps, documented in .env.example and README. Extra slots beyond the cap
# are read anyway (refusing a key the operator clearly meant to use would be
# obnoxious) but warned about, since the provider's own account limits are what
# the cap reflects.
DEFAULT_MAX_SLOTS = 10

# Values that are obviously placeholders rather than keys. .env.example ships
# the names with empty values; someone filling it in halfway would otherwise
# get "invalid key" errors from the provider instead of "no key configured".
_PLACEHOLDERS = {
    "", "changeme", "your-key-here", "your-api-key", "xxx", "none", "null",
}


def _state_dir():
    """Where the cursor file lives.

    MEETING_BOT_ROOT holds only run bookkeeping (runs/, tmp/, state/) now that
    the media directories are configured independently — see lib/paths.py.
    """
    root = os.environ.get("MEETING_BOT_ROOT", "/opt/meeting-bot")
    return Path(os.environ.get("MEETING_BOT_STATE_DIR", str(Path(root) / "state")))


def cursor_file():
    return _state_dir() / "keycursor.json"


def read_env_keys(name, aliases=(), max_slots=DEFAULT_MAX_SLOTS, env=None):
    """Collect the configured keys for `name`, in slot order.

    Accepts the bare name and its aliases as slot 1, then `<name>_1` upward.
    Blank slots are skipped rather than ending the scan, so commenting out
    `..._2` doesn't hide `..._3`. Duplicates are dropped: the same key twice is
    one account, and rotating between two copies of it just looks like it's
    working.
    """
    env = os.environ if env is None else env
    found = []

    def _add(value):
        if value is None:
            return
        value = value.strip()
        if value.lower() in _PLACEHOLDERS:
            return
        if value not in found:
            found.append(value)

    for bare in (name,) + tuple(aliases):
        _add(env.get(bare))

    # Scan a little past the cap so an over-configured file is reported rather
    # than silently truncated.
    for i in range(1, max_slots + 6):
        for base in (name,) + tuple(aliases):
            _add(env.get(f"{base}_{i}"))

    if len(found) > max_slots:
        print(
            f"warning: {len(found)} {name} values configured but only "
            f"{max_slots} are supported by the provider account limits — "
            f"using all of them anyway",
            file=sys.stderr,
        )
    return found


class KeyRing:
    """A named list of API keys plus a persisted round-robin cursor."""

    def __init__(self, name, keys, cursor_path=None):
        self.name = name
        self.keys = list(keys)
        self.cursor_path = Path(cursor_path) if cursor_path else cursor_file()

    @classmethod
    def from_env(cls, name, aliases=(), max_slots=DEFAULT_MAX_SLOTS,
                 env=None, cursor_path=None):
        return cls(name, read_env_keys(name, aliases, max_slots, env),
                   cursor_path=cursor_path)

    def __len__(self):
        return len(self.keys)

    def __bool__(self):
        return bool(self.keys)

    # --- cursor persistence --------------------------------------------------
    def _read_cursor(self):
        try:
            data = json.loads(self.cursor_path.read_text())
        except (OSError, ValueError):
            return 0
        try:
            return int(data.get(self.name, 0))
        except (TypeError, ValueError):
            return 0

    def _write_cursor(self, value):
        """Persist the cursor. Best-effort — never raises.

        Read-modify-write under an flock because several stages (and several
        pipeline sessions) share this one file, and a lost update here would
        silently pin two providers to the same starting key.
        """
        try:
            self.cursor_path.parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            return
        try:
            import fcntl
            lock_path = self.cursor_path.with_suffix(".lock")
            with open(lock_path, "a+") as lock:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
                try:
                    data = json.loads(self.cursor_path.read_text())
                    if not isinstance(data, dict):
                        data = {}
                except (OSError, ValueError):
                    data = {}
                data[self.name] = int(value)
                tmp = self.cursor_path.with_suffix(".tmp")
                tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
                tmp.replace(self.cursor_path)
                try:
                    os.chmod(self.cursor_path, 0o600)
                except OSError:
                    pass
        except OSError as exc:
            print(f"warning: could not persist {self.name} key cursor: {exc}",
                  file=sys.stderr)

    # --- rotation ------------------------------------------------------------
    def start_slot(self):
        """The slot this process should try first."""
        if not self.keys:
            return 0
        return self._read_cursor() % len(self.keys)

    def rotate(self):
        """Yield (slot, key) for every key, starting at the persisted cursor.

        The caller breaks out on success and calls commit(slot). Exhausting the
        generator means every key failed.
        """
        if not self.keys:
            return
        start = self.start_slot()
        for i in range(len(self.keys)):
            slot = (start + i) % len(self.keys)
            yield slot, self.keys[slot]

    def commit(self, slot):
        """Record that `slot` was used, so the next process starts after it."""
        if not self.keys:
            return
        self._write_cursor((int(slot) + 1) % len(self.keys))

    def current(self):
        """The key at the cursor, without advancing. None when unconfigured."""
        if not self.keys:
            return None
        return self.keys[self.start_slot()]

    def label(self, slot):
        """Human-readable "key 2/3" for logs. Never includes the key itself."""
        return f"key {int(slot) + 1}/{len(self.keys)}"


def missing_keys_message(name, max_slots, extra=""):
    """The "where do I put my key?" error text, shared by all three clients."""
    slots = ", ".join(f"{name}_{i}" for i in range(1, min(max_slots, 3) + 1))
    tail = f"\n{extra}" if extra else ""
    return (
        f"No {name} configured.\n\n"
        f"Add at least one key to the .env file at the repo root:\n\n"
        f"    {name}_1=your-key-here\n\n"
        f"Up to {max_slots} numbered slots are rotated round-robin to spread "
        f"quota across accounts ({slots}, ...). The unnumbered {name} is also "
        f"accepted and counts as slot 1.{tail}"
    )


def _main(argv):
    """CLI: `python3 lib/keyring.py status` — how many keys each ring has.

    Prints counts and the current slot, never the keys themselves, so it is
    safe to paste into a bug report.
    """
    rings = [
        ("ANTHROPIC_API_KEY", (), 1),
        ("GEMINI_API_KEY", ("GOOGLE_API_KEY",), 3),
        ("ASSEMBLYAI_API_KEY", (), 3),
        ("YT_TRANSCRIPT_KEY", (), 10),
    ]
    if len(argv) > 1 and argv[1] != "status":
        print(f"Usage: {argv[0]} status", file=sys.stderr)
        return 2
    print(f"cursor file: {cursor_file()}")
    for name, aliases, cap in rings:
        ring = KeyRing.from_env(name, aliases=aliases, max_slots=cap)
        if ring:
            print(f"  {name:<22} {len(ring)} key(s), next = "
                  f"{ring.label(ring.start_slot())}")
        else:
            print(f"  {name:<22} (none configured)")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv))
