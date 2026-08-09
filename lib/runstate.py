#!/usr/bin/env python3
"""
Per-run state for the pipeline: what stage got how far, and where its output
landed. This is what makes `pipeline.sh` resumable and safe to run twice at
once.

State lives at /opt/meeting-bot/runs/<run_id>/state.json. One directory per
run holds everything mutable about it:

    runs/<run_id>/
      state.json          stage statuses + artifact paths (this file's job)
      state.lock          flock target; guards read-modify-write
      logs/<stage>.log    stdout+stderr of each stage
      kill                kill sentinel (per-run, so one meeting's kill
                          switch can't take down another's)
      admitted            admission marker, same reasoning
      video.mp4           YouTube download, when applicable

Statuses are: pending | running | done | failed.

RESUME SEMANTICS. `status` reports `done` only if the stage is marked done AND
every artifact it recorded still exists on disk. Someone who deletes a
transcript and re-runs expects it to be regenerated, not skipped — a state file
that disagrees with the filesystem is worse than no state file at all.

CONCURRENCY. Every mutation takes an exclusive flock on state.lock and does a
read-modify-write, so two stages finishing simultaneously (transcribe and
frames run in parallel) can't clobber each other's entries. Writes go to a
temp file and get renamed, so a crash mid-write can't leave truncated JSON.

CLI (all subcommands take --run-dir, except `latest`/`list` which take --root):

    runstate.py init    --run-dir D --input U --input-type T --name N \
                        --safe-name S [--language L] [--prompt P]
    runstate.py status  --run-dir D --stage S      -> prints pending|running|done|failed
    runstate.py start   --run-dir D --stage S
    runstate.py done    --run-dir D --stage S [--artifact k=v]...
    runstate.py fail    --run-dir D --stage S [--error MSG]
    runstate.py get     --run-dir D --key stages.transcribe.artifacts.txt
    runstate.py reset   --run-dir D [--stage S]    -> back to pending (--force path)
    runstate.py show    --run-dir D                -> human-readable summary
    runstate.py latest  [--root R]                 -> run_id of the newest run
    runstate.py list    [--root R] [--limit N]     -> one line per run
    runstate.py sweep   [--root R] [--days N]      -> delete run dirs older than N days
"""
import argparse
import fcntl
import json
import os
import shutil
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_ROOT = Path(os.environ.get("MEETING_BOT_ROOT", "/opt/meeting-bot")) / "runs"

# Declared in dependency order for display purposes only; the actual DAG lives
# in pipeline.sh. `fetch_video` exists so the YouTube download can run in
# parallel with caption fetching instead of being buried inside summarize.py.
STAGES = ("record", "fetch_video", "transcribe", "frames", "summarize")

PENDING, RUNNING, DONE, FAILED = "pending", "running", "done", "failed"


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class RunState:
    """Read-modify-write access to one run's state.json, guarded by a flock."""

    def __init__(self, run_dir):
        self.run_dir = Path(run_dir)
        self.path = self.run_dir / "state.json"
        self.lock_path = self.run_dir / "state.lock"

    # --- locking ------------------------------------------------------------

    def _locked(self):
        """Context manager yielding an exclusive lock on this run.

        The lock is a separate file from state.json: we replace state.json via
        rename on every write, which would otherwise drop the lock along with
        the old inode.
        """
        run_dir = self.run_dir
        lock_path = self.lock_path

        class _Lock:
            def __enter__(self):
                run_dir.mkdir(parents=True, exist_ok=True)
                self.fh = open(lock_path, "a+")
                fcntl.flock(self.fh, fcntl.LOCK_EX)
                return self

            def __exit__(self, *exc):
                fcntl.flock(self.fh, fcntl.LOCK_UN)
                self.fh.close()
                return False

        return _Lock()

    # --- raw io -------------------------------------------------------------

    def _read(self):
        try:
            return json.loads(self.path.read_text())
        except (OSError, json.JSONDecodeError):
            return {}

    def _write(self, data):
        data["updated_at"] = _now()
        self.run_dir.mkdir(parents=True, exist_ok=True)
        # Temp file in the same directory so the rename is atomic (same fs).
        fd, tmp = tempfile.mkstemp(dir=str(self.run_dir), prefix=".state-", suffix=".json")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(data, f, indent=2, sort_keys=False)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def _mutate(self, fn):
        with self._locked():
            data = self._read()
            fn(data)
            self._write(data)
            return data

    # --- public api ---------------------------------------------------------

    def init(self, **meta):
        """Create state.json if absent; refresh metadata if it already exists.

        Idempotent on purpose: re-running the same command is the normal way to
        resume, so init must not wipe stage progress.
        """
        def _fn(data):
            data.setdefault("run_id", self.run_dir.name)
            data.setdefault("created_at", _now())
            data.setdefault("stages", {})
            for stage in STAGES:
                data["stages"].setdefault(
                    stage, {"status": PENDING, "attempts": 0, "artifacts": {}}
                )
            for key, value in meta.items():
                if value is not None:
                    data[key] = value
        return self._mutate(_fn)

    def stage(self, name):
        return self._read().get("stages", {}).get(name, {})

    def status(self, name, verify=True):
        """Effective status, with `done` downgraded when artifacts vanished."""
        st = self.stage(name)
        status = st.get("status", PENDING)
        if status == DONE and verify:
            for path in st.get("artifacts", {}).values():
                if path and not Path(path).exists():
                    return PENDING
        return status

    def start(self, name):
        def _fn(data):
            st = data.setdefault("stages", {}).setdefault(name, {"artifacts": {}})
            st["status"] = RUNNING
            st["attempts"] = st.get("attempts", 0) + 1
            st["started_at"] = _now()
            st.pop("error", None)
        return self._mutate(_fn)

    def done(self, name, artifacts=None):
        def _fn(data):
            st = data.setdefault("stages", {}).setdefault(name, {})
            st["status"] = DONE
            st["ended_at"] = _now()
            st.pop("error", None)
            if artifacts:
                st.setdefault("artifacts", {}).update(artifacts)
        return self._mutate(_fn)

    def fail(self, name, error=None):
        def _fn(data):
            st = data.setdefault("stages", {}).setdefault(name, {})
            st["status"] = FAILED
            st["ended_at"] = _now()
            if error:
                # Keep it bounded: a stack trace piped in here shouldn't bloat
                # the state file that every subsequent command has to parse.
                st["error"] = error[:2000]
        return self._mutate(_fn)

    def reset(self, name=None):
        """Send one stage (or all of them) back to pending.

        Artifacts are forgotten too, otherwise `status` would keep reporting a
        stale `done` off the old paths.
        """
        def _fn(data):
            targets = [name] if name else list(data.get("stages", {}).keys())
            for target in targets:
                data.setdefault("stages", {})[target] = {
                    "status": PENDING, "attempts": 0, "artifacts": {}
                }
        return self._mutate(_fn)

    def get(self, dotted_key):
        node = self._read()
        for part in dotted_key.split("."):
            if not isinstance(node, dict) or part not in node:
                return None
            node = node[part]
        return node


# --- CLI --------------------------------------------------------------------


def _parse_artifacts(pairs):
    out = {}
    for pair in pairs or []:
        if "=" not in pair:
            raise SystemExit(f"--artifact expects key=value, got: {pair!r}")
        key, value = pair.split("=", 1)
        out[key] = value
    return out


def _iter_runs(root):
    root = Path(root)
    if not root.is_dir():
        return []
    runs = [d for d in root.iterdir() if (d / "state.json").is_file()]
    # Newest first, by state.json mtime — run_ids carry a timestamp, but mtime
    # also reflects a resume, which is what "latest" should mean here.
    runs.sort(key=lambda d: (d / "state.json").stat().st_mtime, reverse=True)
    return runs


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    sub = ap.add_subparsers(dest="cmd", required=True)

    def with_run_dir(p):
        p.add_argument("--run-dir", required=True)
        return p

    p = with_run_dir(sub.add_parser("init"))
    p.add_argument("--input")
    p.add_argument("--input-type")
    p.add_argument("--name")
    p.add_argument("--safe-name")
    p.add_argument("--language")
    p.add_argument("--prompt")
    p.add_argument("--display-name")

    p = with_run_dir(sub.add_parser("status"))
    p.add_argument("--stage", required=True)
    p.add_argument("--no-verify", action="store_true",
                   help="report the recorded status even if artifacts are gone")

    p = with_run_dir(sub.add_parser("start"))
    p.add_argument("--stage", required=True)

    p = with_run_dir(sub.add_parser("done"))
    p.add_argument("--stage", required=True)
    p.add_argument("--artifact", action="append", metavar="KEY=VALUE")

    p = with_run_dir(sub.add_parser("fail"))
    p.add_argument("--stage", required=True)
    p.add_argument("--error")

    p = with_run_dir(sub.add_parser("get"))
    p.add_argument("--key", required=True)

    p = with_run_dir(sub.add_parser("reset"))
    p.add_argument("--stage")

    with_run_dir(sub.add_parser("show"))

    p = sub.add_parser("latest")
    p.add_argument("--root", default=str(DEFAULT_ROOT))

    p = sub.add_parser("find")
    p.add_argument("--root", default=str(DEFAULT_ROOT))
    p.add_argument("--input", required=True)
    p.add_argument("--incomplete", action="store_true",
                   help="only match runs that haven't finished summarizing")

    p = sub.add_parser("list")
    p.add_argument("--root", default=str(DEFAULT_ROOT))
    p.add_argument("--limit", type=int, default=20)

    p = sub.add_parser("sweep")
    p.add_argument("--root", default=str(DEFAULT_ROOT))
    p.add_argument("--days", type=float, default=30.0)

    args = ap.parse_args()

    if args.cmd == "latest":
        runs = _iter_runs(args.root)
        if not runs:
            print("", end="")
            return 1
        print(runs[0].name)
        return 0

    if args.cmd == "find":
        # Powers auto-resume: re-running the same command should pick up the
        # run that didn't finish rather than starting a fresh one. Matching is
        # on the exact input string, newest first.
        for d in _iter_runs(args.root):
            st = RunState(d)
            if st.get("input") != args.input:
                continue
            if args.incomplete and st.status("summarize") == DONE:
                continue
            print(d.name)
            return 0
        return 1

    if args.cmd == "list":
        runs = _iter_runs(args.root)[: args.limit]
        if not runs:
            print("No runs found under " + str(args.root))
            return 0
        print(f"{'RUN ID':<44} {'UPDATED':<22} STAGES")
        for d in runs:
            st = RunState(d)
            data = st._read()
            marks = []
            for stage in STAGES:
                s = data.get("stages", {}).get(stage, {}).get("status", PENDING)
                marks.append({
                    DONE: "+", FAILED: "!", RUNNING: ">", PENDING: "."
                }.get(s, "?") + stage[:4])
            print(f"{d.name:<44} {data.get('updated_at', '?'):<22} {' '.join(marks)}")
        return 0

    if args.cmd == "sweep":
        # Old run dirs hold the YouTube video download, which is the only large
        # thing in here. Nothing else in the pipeline garbage-collects them.
        cutoff = time.time() - args.days * 86400
        removed = 0
        for d in _iter_runs(args.root):
            if (d / "state.json").stat().st_mtime < cutoff:
                shutil.rmtree(d, ignore_errors=True)
                removed += 1
        print(f"Swept {removed} run dir(s) older than {args.days} days")
        return 0

    state = RunState(args.run_dir)

    if args.cmd == "init":
        state.init(input=args.input, input_type=args.input_type, name=args.name,
                   safe_name=args.safe_name, language=args.language,
                   prompt=args.prompt, display_name=args.display_name)
        return 0

    if args.cmd == "status":
        print(state.status(args.stage, verify=not args.no_verify))
        return 0

    if args.cmd == "start":
        state.start(args.stage)
        return 0

    if args.cmd == "done":
        state.done(args.stage, _parse_artifacts(args.artifact))
        return 0

    if args.cmd == "fail":
        state.fail(args.stage, args.error)
        return 0

    if args.cmd == "get":
        value = state.get(args.key)
        if value is None:
            return 1
        print(value if not isinstance(value, (dict, list))
              else json.dumps(value))
        return 0

    if args.cmd == "reset":
        state.reset(args.stage)
        return 0

    if args.cmd == "show":
        data = state._read()
        if not data:
            print(f"No state at {state.path}", file=sys.stderr)
            return 1
        print(f"run_id:     {data.get('run_id')}")
        print(f"input:      {data.get('input')}  ({data.get('input_type')})")
        print(f"name:       {data.get('name')}  (safe: {data.get('safe_name')})")
        print(f"language:   {data.get('language')}   prompt: {data.get('prompt') or '(default)'}")
        print(f"created:    {data.get('created_at')}")
        print(f"updated:    {data.get('updated_at')}")
        print("stages:")
        for stage in STAGES:
            st = data.get("stages", {}).get(stage, {})
            line = f"  {stage:<12} {st.get('status', PENDING):<8} attempts={st.get('attempts', 0)}"
            print(line)
            for key, value in (st.get("artifacts") or {}).items():
                exists = "" if Path(value).exists() else "   [MISSING]"
                print(f"      {key}: {value}{exists}")
            if st.get("error"):
                print(f"      error: {st['error'].splitlines()[0][:160]}")
        return 0

    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
