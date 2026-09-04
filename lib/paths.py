#!/usr/bin/env python3
"""
Output-directory resolution.

The five media directories are configured independently — there is no implied
layout under one root:

    RECORDINGS_DIR    the MP4s stage 1 produces
    TRANSCRIPTS_DIR   the .txt/.srt pairs stage 2 produces
    FRAMES_DIR        per-run frame directories + manifest.json
    SUMMARIES_DIR     the markdown summaries stage 3 produces
    PDF_DIR           the rendered PDFs stage 3 produces

That means any of them can point at a different disk or a network mount
(summaries on a NAS, recordings on the big spindle) without moving the others.
MEETING_BOT_ROOT is NOT their parent any more; it holds only bookkeeping that
the pipeline itself owns — runs/, tmp/, state/, chrome-profile/.

All five are REQUIRED. An unset one raises a message naming it rather than
quietly writing to a default nobody configured — with independent paths, a
wrong default doesn't fail, it just puts your lecture summaries somewhere you
won't look. `.env.example` ships every one of them filled in, so a copied .env
works with no edits.

Paths may contain spaces. Nothing here splits on whitespace, and the bash
counterpart (lib/paths.sh) quotes every expansion.
"""
import os
from pathlib import Path

# env var -> what it's for, used in the error message.
MEDIA_DIRS = {
    "RECORDINGS_DIR": "meeting recordings (.mp4)",
    "TRANSCRIPTS_DIR": "transcripts (.txt/.srt)",
    "FRAMES_DIR": "extracted video frames",
    "SUMMARIES_DIR": "summary documents (.md)",
    "PDF_DIR": "rendered summary PDFs",
}

# Accepted older names, so an existing .env doesn't break on upgrade.
_ALIASES = {
    "FRAMES_DIR": ("FRAME_OUTPUT_DIR",),
}


class MissingPathError(RuntimeError):
    """A required output directory isn't configured."""


def _lookup(name, env):
    value = env.get(name)
    if value and value.strip():
        return value.strip()
    for alias in _ALIASES.get(name, ()):
        value = env.get(alias)
        if value and value.strip():
            return value.strip()
    return None


def get_dir(name, env=None, create=False):
    """Resolve one configured directory. Raises MissingPathError if unset."""
    if name not in MEDIA_DIRS:
        raise KeyError(f"unknown output directory: {name}")
    env = os.environ if env is None else env
    value = _lookup(name, env)
    if not value:
        raise MissingPathError(
            f"{name} is not set. It configures where {MEDIA_DIRS[name]} are "
            f"written.\n"
            f"  Add it to the .env file at the repo root (see .env.example), "
            f"e.g.\n"
            f"      {name}=/srv/meeting-bot/{name.split('_')[0].lower()}\n"
            f"  The five output directories are independent — "
            f"{', '.join(sorted(MEDIA_DIRS))} — so each one has to be set."
        )
    path = Path(value).expanduser()
    if create:
        path.mkdir(parents=True, exist_ok=True)
    return path


def all_dirs(env=None, create=False):
    """Resolve all five at once. Reports every missing var, not just the first."""
    env = os.environ if env is None else env
    resolved = {}
    missing = []
    for name in MEDIA_DIRS:
        try:
            resolved[name] = get_dir(name, env=env, create=create)
        except MissingPathError:
            missing.append(name)
    if missing:
        raise MissingPathError(
            "These output directories are not configured: "
            + ", ".join(missing)
            + "\n  Set them in the .env file at the repo root — see "
              ".env.example, which ships a working set of defaults."
        )
    return resolved


def bot_root(env=None):
    """Where the pipeline's own bookkeeping lives (runs/, tmp/, state/)."""
    env = os.environ if env is None else env
    return Path(env.get("MEETING_BOT_ROOT", "/opt/meeting-bot")).expanduser()


def _main(argv):
    """CLI: `python3 lib/paths.py show` prints the resolved paths (or what's
    missing), and `... ensure` creates them."""
    import sys
    cmd = argv[1] if len(argv) > 1 else "show"
    if cmd not in ("show", "ensure"):
        print(f"Usage: {argv[0]} [show|ensure]", file=sys.stderr)
        return 2
    try:
        dirs = all_dirs(create=(cmd == "ensure"))
    except MissingPathError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(f"{'MEETING_BOT_ROOT':<18} {bot_root()}")
    for name, path in dirs.items():
        print(f"{name:<18} {path}")
    return 0


if __name__ == "__main__":
    import sys
    raise SystemExit(_main(sys.argv))
