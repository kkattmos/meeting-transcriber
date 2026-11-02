#!/usr/bin/env python3
"""Run history housekeeping: clear runs, remove their caches, edit their tags
(settled with the operator 2026-11-02, for the web UI's Runs list).

Stdlib only, so trigger_server.py imports it directly.

* **Clear** removes the run's bookkeeping — `runs/<id>/` (state, logs, the
  chunk cache, any download) — and its frames (`FRAMES_DIR/<id>`, which
  nothing else can reach once the run is gone), so it disappears from the
  list. The deliverables are never touched: the recording, the transcript,
  the summary and the PDF stay in their output folders.
* **Cache** removes, per run, any of: `frames` (FRAMES_DIR/<id>, including
  the model's downscaled llm-* copies), `chunks` (runs/<id>/chunks, the
  finished chunk summaries a resume reuses) and `media` (runs/<id>/video.*,
  clip.mp4, .part files — a YouTube/Kaltura download). Each is regenerable;
  the stages that made them are marked `cleaned` (as the post-summary sweep
  does), so a finished run stays finished and a resume makes them again.
  And globally: `tmp` ($MEETING_BOT_ROOT/tmp — rejected answers, prompt
  files, the CLI's scratch cwd, blank media), all recreated on demand.
* **Tags** are the operator's own labels (`tags` in state.json).

A run being processed (its run.lock owner alive) is never cleared or swept,
and `tmp` is not swept while any run is. A member of a --combine set whose
combine run still exists is not cleared on its own — its combine run reads
its transcript and frames; clear the combine run (with its members) instead.
A combine run cleared without its members leaves them pointing at it, which
keeps --resume-all from billing them individual summaries; re-running the
same --combine command makes a new combine run over them.

CLI:
    runadmin.py clear  --run-id ID [--with-members]
    runadmin.py cache  --run-id ID --what frames,chunks,media
    runadmin.py tmp
    runadmin.py sizes  --run-id ID
    runadmin.py tags   --run-id ID [--set TAG]...
Each prints a JSON object. MEETING_BOT_ROOT and FRAMES_DIR come from the
environment (source .env first, or run it through pipeline.sh's loader).
"""
import argparse
import json
import os
import re
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from procutil import pid_alive as _proc_alive  # noqa: E402
from runstate import RunState, normalize_tags  # noqa: E402

RUN_ID_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
CACHE_KINDS = ("frames", "chunks", "media")
MEDIA_GLOBS = ("video.*", "clip.mp4", "clip.part.mp4", "*.part")


class AdminError(Exception):
    pass


def bot_root(env=None):
    env = os.environ if env is None else env
    return Path(env.get("MEETING_BOT_ROOT") or os.path.expanduser("~/.local/share/meeting-bot"))


def frames_root(env=None):
    env = os.environ if env is None else env
    value = (env.get("FRAMES_DIR") or "").strip()
    return Path(os.path.expanduser(value)) if value else None


def _pid_alive(pid):
    return _proc_alive(pid)


def is_active(run_dir):
    try:
        owner = (Path(run_dir) / "run.lock" / "pid").read_text().strip()
    except OSError:
        return False
    return owner.isdigit() and _pid_alive(owner)


def _size(path):
    path = Path(path)
    if path.is_file():
        try:
            return path.stat().st_size
        except OSError:
            return 0
    total = 0
    for dirpath, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += os.lstat(os.path.join(dirpath, name)).st_size
            except OSError:
                pass
    return total


def _remove(path):
    size = _size(path)
    if Path(path).is_dir() and not Path(path).is_symlink():
        shutil.rmtree(path, ignore_errors=True)
    else:
        try:
            Path(path).unlink()
        except FileNotFoundError:
            pass
    return size


def _state(run_dir):
    try:
        return json.loads((Path(run_dir) / "state.json").read_text())
    except (OSError, ValueError):
        return None


def run_dir_for(runs_root, run_id):
    if not run_id or not RUN_ID_RE.match(run_id) or run_id in (".", ".."):
        raise AdminError(f"not a run id: {run_id!r}")
    d = Path(runs_root) / run_id
    if not (d / "state.json").is_file():
        raise AdminError(f"no such run: {run_id}")
    return d


def _frames_dir(frames, run_id):
    if frames is None:
        return None
    d = frames / run_id
    # Never FRAMES_DIR itself, never outside it.
    return d if d.parent == frames and d.name == run_id else None


def _media_files(run_dir):
    out = []
    for pattern in MEDIA_GLOBS:
        out.extend(p for p in Path(run_dir).glob(pattern) if p.is_file())
    return sorted(set(out))


def sizes(run_dir, frames=None):
    run_dir = Path(run_dir)
    fd = _frames_dir(frames, run_dir.name)
    return {
        "frames": _size(fd) if fd and fd.is_dir() else 0,
        "chunks": _size(run_dir / "chunks") if (run_dir / "chunks").is_dir() else 0,
        "media": sum(_size(p) for p in _media_files(run_dir)),
        "bookkeeping": _size(run_dir),
    }


def remove_cache(run_dir, what, frames=None):
    """Remove the named caches of one run; returns {kind: bytes freed}."""
    run_dir = Path(run_dir)
    what = [w for w in what if w]
    bad = [w for w in what if w not in CACHE_KINDS]
    if bad or not what:
        raise AdminError(f"cache kinds are {', '.join(CACHE_KINDS)} (got {', '.join(bad) or 'none'})")
    if is_active(run_dir):
        raise AdminError(f"{run_dir.name} is being processed; stop it or wait")
    state = RunState(run_dir)
    freed = {}
    if "frames" in what:
        fd = _frames_dir(frames, run_dir.name)
        freed["frames"] = _remove(fd) if fd and fd.exists() else 0
        state.cleaned("frames")
    if "chunks" in what:
        freed["chunks"] = _remove(run_dir / "chunks") if (run_dir / "chunks").exists() else 0
    if "media" in what:
        data = _state(run_dir) or {}
        n = 0
        if data.get("input_type") != "meeting":   # a recording is never in here anyway
            n = sum(_remove(p) for p in _media_files(run_dir))
        freed["media"] = n
        state.cleaned("fetch_video")
        state.cleaned("clip")
    return freed


def clear(runs_root, run_id, frames=None, with_members=False):
    """Remove one run's bookkeeping (and frames). Returns what went."""
    run_dir = run_dir_for(runs_root, run_id)
    data = _state(run_dir) or {}
    if is_active(run_dir):
        raise AdminError(f"{run_id} is being processed; stop it before clearing it")
    parent = data.get("combined_into")
    if parent and (Path(runs_root) / parent / "state.json").is_file():
        raise AdminError(f"{run_id} is part of the combined set {parent}; "
                         "clear that run (with its members) instead")
    members = (data.get("members") or []) if data.get("input_type") == "combine" else []
    member_dirs = []
    if with_members:
        for m in members:
            try:
                md = run_dir_for(runs_root, m)
            except AdminError:
                continue  # already gone
            if is_active(md):
                raise AdminError(f"member {m} is being processed; stop it first")
            member_dirs.append(md)
    cleared, freed = [], 0
    for d in [run_dir, *member_dirs]:
        fd = _frames_dir(frames, d.name)
        if fd and fd.exists():
            freed += _remove(fd)
        freed += _remove(d)
        cleared.append(d.name)
    return {"cleared": cleared, "bytes": freed,
            "members_kept": [] if with_members else list(members)}


def clear_tmp(root, runs_root=None):
    runs_root = Path(runs_root or Path(root) / "runs")
    busy = [d.name for d in runs_root.glob("*") if d.is_dir() and is_active(d)]
    if busy:
        raise AdminError(f"not while a run is being processed ({', '.join(busy[:3])})")
    tmp = Path(root) / "tmp"
    freed = 0
    if tmp.is_dir():
        for entry in tmp.iterdir():
            freed += _remove(entry)
    return {"bytes": freed}


def tmp_size(root):
    tmp = Path(root) / "tmp"
    return _size(tmp) if tmp.is_dir() else 0


def set_tags(run_dir, tags):
    tags = normalize_tags(tags)
    RunState(run_dir).init(tags=tags)
    return {"tags": tags}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("clear")
    p.add_argument("--run-id", required=True)
    p.add_argument("--with-members", action="store_true")
    p = sub.add_parser("cache")
    p.add_argument("--run-id", required=True)
    p.add_argument("--what", required=True)
    sub.add_parser("tmp")
    p = sub.add_parser("sizes")
    p.add_argument("--run-id", required=True)
    p = sub.add_parser("tags")
    p.add_argument("--run-id", required=True)
    p.add_argument("--set", action="append", default=None)
    args = ap.parse_args(argv)
    root = bot_root()
    runs = root / "runs"
    frames = frames_root()
    try:
        if args.cmd == "clear":
            out = clear(runs, args.run_id, frames, args.with_members)
        elif args.cmd == "cache":
            out = remove_cache(run_dir_for(runs, args.run_id),
                               args.what.split(","), frames)
        elif args.cmd == "tmp":
            out = clear_tmp(root, runs)
        elif args.cmd == "sizes":
            out = sizes(run_dir_for(runs, args.run_id), frames)
        else:
            d = run_dir_for(runs, args.run_id)
            out = (set_tags(d, args.set) if args.set is not None
                   else {"tags": (_state(d) or {}).get("tags") or []})
    except AdminError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(out, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
