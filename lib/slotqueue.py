#!/usr/bin/env python3
"""
A FIFO slot queue shared by every pipeline invocation on this machine.

The problem: `--jobs` only limits concurrency *within one* `./pipeline.sh`
invocation. Run the script in three terminals (or trigger it three times from
your phone) and you get three independent sets of stages all hitting the same
4 vCPUs and the same rate-limited APIs at once. This queue is the machine-wide
throttle those separate sessions coordinate through.

Each component (`record`, `fetch_video`, `transcribe`, `frames`, `summarize`)
gets its own queue with its own slot count. A session that can't get a slot
waits its turn and says how many are ahead of it, so a blocked run never looks
hung.

    QUEUE_SLOTS_TRANSCRIBE=1     one transcription at a time, box-wide
    QUEUE_SLOTS_FRAMES=1         one ffmpeg frame extraction at a time
    QUEUE_SLOTS_SUMMARIZE=2      two summaries in flight
    QUEUE_SLOTS_DEFAULT=1        fallback for any component not named above

**Every component is unlimited unless you set its variable.** With nothing
configured this code does no file I/O at all and behaves exactly as before —
you opt into serialization where you want it.

    Careful with QUEUE_SLOTS_RECORD. Recording is the one time-sensitive
    stage: if two meetings overlap and only one record slot exists, the second
    meeting is not delayed, it is *missed* — you cannot record it later. Leave
    it unlimited unless your meetings never overlap.

## How a slot is held

`acquire` records the **calling shell's** PID as the holder and exits, rather
than staying alive itself. That's what lets a bash script hold a slot across a
long stage without a babysitter process. A holder whose PID is gone is pruned
by the next caller, so a crashed or SIGKILL'd run releases its slot
automatically — nothing can wedge the queue permanently.

## CLI

    slotqueue.py acquire --component frames --pid $BASHPID [--label run_id]
        Blocks until a slot is free, prints the ticket number on stdout.
        Prints "waiting" progress to stderr.

    slotqueue.py release --component frames --ticket 7

    slotqueue.py run --component frames -- ffmpeg ...
        acquire + run + release, for callers that have a real command.

    slotqueue.py status          what is running and what is waiting
    slotqueue.py reset [--component X]   clear a queue (stale-state escape hatch)
"""
import argparse
import errno
import fcntl
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

COMPONENTS = ("record", "fetch_video", "transcribe", "frames", "summarize")

BOT_ROOT = Path(os.environ.get("MEETING_BOT_ROOT", "/opt/meeting-bot"))
QUEUE_DIR = Path(os.environ.get("QUEUE_DIR", str(BOT_ROOT / "queue")))

POLL_SECONDS = float(os.environ.get("QUEUE_POLL_SECONDS", "2"))
# 0 = wait forever, which is the default: a queued stage should run eventually,
# not fail because the box was busy.
WAIT_TIMEOUT = float(os.environ.get("QUEUE_WAIT_TIMEOUT_SECONDS", "0"))

UNLIMITED = 0


def slots_for(component):
    """Slot count for a component. 0 (or unset/invalid) means unlimited."""
    specific = os.environ.get(f"QUEUE_SLOTS_{component.upper()}")
    fallback = os.environ.get("QUEUE_SLOTS_DEFAULT")
    raw = specific if specific not in (None, "") else fallback
    if raw in (None, ""):
        return UNLIMITED
    try:
        value = int(raw)
    except ValueError:
        print(f"  warning: QUEUE_SLOTS_{component.upper()}={raw!r} is not a "
              f"number — treating as unlimited", file=sys.stderr)
        return UNLIMITED
    return max(0, value)


def _alive(pid):
    """Is this PID still running?

    Signal 0 checks for existence without delivering anything. EPERM means the
    process exists but belongs to another user — still alive, so still holding.
    """
    try:
        os.kill(int(pid), 0)
    except OSError as exc:
        return exc.errno == errno.EPERM
    except (TypeError, ValueError):
        return False
    return True


class Queue:
    """One component's queue, stored as a JSON file guarded by a lock file."""

    def __init__(self, component, queue_dir=None):
        self.component = component
        self.dir = Path(queue_dir) if queue_dir else QUEUE_DIR
        self.path = self.dir / f"{component}.json"
        self.lock_path = self.dir / f"{component}.lock"

    # --- storage ------------------------------------------------------------

    def _locked(self):
        directory, lock_path = self.dir, self.lock_path

        class _Lock:
            def __enter__(self):
                directory.mkdir(parents=True, exist_ok=True)
                self.fh = open(lock_path, "a+")
                fcntl.flock(self.fh, fcntl.LOCK_EX)
                return self

            def __exit__(self, *exc):
                fcntl.flock(self.fh, fcntl.LOCK_UN)
                self.fh.close()
                return False

        return _Lock()

    def _read(self):
        try:
            data = json.loads(self.path.read_text())
        except (OSError, json.JSONDecodeError):
            data = {}
        data.setdefault("next_ticket", 1)
        data.setdefault("holders", [])
        data.setdefault("waiting", [])
        return data

    def _write(self, data):
        self.dir.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(self.dir), prefix=f".{self.component}-")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(data, f, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    @staticmethod
    def _prune(data):
        """Drop entries whose process is gone.

        This is the whole recovery story: a run that was SIGKILL'd, or a box
        that rebooted mid-stage, leaves a holder behind. Rather than needing a
        cleanup daemon, the next caller notices the PID is dead and reclaims
        the slot.
        """
        before = len(data["holders"]) + len(data["waiting"])
        data["holders"] = [h for h in data["holders"] if _alive(h.get("pid"))]
        data["waiting"] = [w for w in data["waiting"] if _alive(w.get("pid"))]
        return before - (len(data["holders"]) + len(data["waiting"]))

    # --- public api ---------------------------------------------------------

    def acquire(self, pid, label="", limit=None, poll=POLL_SECONDS,
                timeout=WAIT_TIMEOUT, on_wait=None):
        """Take a slot, blocking FIFO until one is free. Returns the ticket.

        `pid` is the process that will *hold* the slot — normally the calling
        shell, not this Python process.
        """
        limit = slots_for(self.component) if limit is None else limit
        if limit == UNLIMITED:
            return None  # nothing to coordinate; no files touched

        ticket = None
        started = time.time()
        last_position = None

        while True:
            with self._locked():
                data = self._read()
                self._prune(data)

                if ticket is None:
                    ticket = data["next_ticket"]
                    data["next_ticket"] = ticket + 1
                    data["waiting"].append({
                        "ticket": ticket, "pid": int(pid),
                        "label": label, "since": time.time(),
                    })

                queue = sorted(data["waiting"], key=lambda w: w["ticket"])
                position = next(
                    (i for i, w in enumerate(queue) if w["ticket"] == ticket),
                    None,
                )

                # Our holder died while we were waiting — stop queueing on its
                # behalf rather than taking a slot nobody will use or release.
                if not _alive(pid):
                    data["waiting"] = [w for w in data["waiting"]
                                       if w["ticket"] != ticket]
                    self._write(data)
                    raise SystemExit(
                        f"queue: holder process {pid} exited while waiting")

                if position == 0 and len(data["holders"]) < limit:
                    data["waiting"] = [w for w in data["waiting"]
                                       if w["ticket"] != ticket]
                    data["holders"].append({
                        "ticket": ticket, "pid": int(pid),
                        "label": label, "since": time.time(),
                    })
                    self._write(data)
                    return ticket

                self._write(data)
                ahead = position if position is not None else 0
                running = len(data["holders"])

            if on_wait and ahead != last_position:
                on_wait(ahead, running, limit)
                last_position = ahead

            if timeout and (time.time() - started) > timeout:
                self.release(ticket, pid)
                raise SystemExit(
                    f"queue: timed out after {timeout:.0f}s waiting for a "
                    f"'{self.component}' slot. Raise QUEUE_SLOTS_"
                    f"{self.component.upper()} or clear a stuck queue with "
                    f"'slotqueue.py status'."
                )
            time.sleep(poll)

    def release(self, ticket, pid=None):
        """Give the slot back. Safe to call twice, or on a ticket we never got."""
        if ticket is None:
            return
        with self._locked():
            data = self._read()
            self._prune(data)
            data["holders"] = [h for h in data["holders"]
                               if h.get("ticket") != ticket]
            data["waiting"] = [w for w in data["waiting"]
                               if w.get("ticket") != ticket]
            self._write(data)

    def snapshot(self):
        with self._locked():
            data = self._read()
            self._prune(data)
            self._write(data)
            return data


# --- CLI --------------------------------------------------------------------


def _wait_reporter(component):
    def report(ahead, running, limit):
        if ahead == 0:
            print(f"  queue: waiting for a '{component}' slot "
                  f"({running}/{limit} in use)", file=sys.stderr)
        else:
            print(f"  queue: waiting for a '{component}' slot "
                  f"({ahead} ahead, {running}/{limit} in use)", file=sys.stderr)
    return report


def _fmt_age(since):
    seconds = int(time.time() - float(since or 0))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m{seconds % 60:02d}s"
    return f"{seconds // 3600}h{(seconds % 3600) // 60:02d}m"


def main():
    ap = argparse.ArgumentParser(description="Machine-wide component queue")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("acquire")
    p.add_argument("--component", required=True)
    p.add_argument("--pid", required=True, type=int,
                   help="PID that will hold the slot (usually $BASHPID)")
    p.add_argument("--label", default="")

    p = sub.add_parser("release")
    p.add_argument("--component", required=True)
    p.add_argument("--ticket")

    p = sub.add_parser("run")
    p.add_argument("--component", required=True)
    p.add_argument("--label", default="")
    p.add_argument("command", nargs=argparse.REMAINDER)

    p = sub.add_parser("status")
    p.add_argument("--component")

    p = sub.add_parser("reset")
    p.add_argument("--component")

    args = ap.parse_args()

    if args.cmd == "acquire":
        queue = Queue(args.component)
        ticket = queue.acquire(pid=args.pid, label=args.label,
                               on_wait=_wait_reporter(args.component))
        # Empty line when unlimited, so the caller's $(...) is empty and it
        # knows there's nothing to release.
        print("" if ticket is None else ticket)
        return 0

    if args.cmd == "release":
        if args.ticket:
            Queue(args.component).release(int(args.ticket))
        return 0

    if args.cmd == "run":
        command = [a for a in args.command if a != "--"]
        if not command:
            raise SystemExit("slotqueue.py run: no command given after --")
        queue = Queue(args.component)
        ticket = queue.acquire(pid=os.getpid(), label=args.label,
                               on_wait=_wait_reporter(args.component))
        proc = None

        def _forward(signum, _frame):
            if proc and proc.poll() is None:
                proc.send_signal(signum)

        for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGQUIT):
            signal.signal(sig, _forward)

        try:
            proc = subprocess.Popen(command)
            return proc.wait()
        finally:
            queue.release(ticket)

    if args.cmd == "status":
        components = [args.component] if args.component else list(COMPONENTS)
        any_configured = False
        for name in components:
            limit = slots_for(name)
            queue = Queue(name)
            data = queue.snapshot() if queue.path.exists() else {
                "holders": [], "waiting": []}
            limit_str = "unlimited" if limit == UNLIMITED else str(limit)
            if limit != UNLIMITED:
                any_configured = True
            holders, waiting = data.get("holders", []), data.get("waiting", [])
            if limit == UNLIMITED and not holders and not waiting:
                continue
            print(f"{name}  (slots: {limit_str})")
            for h in sorted(holders, key=lambda x: x.get("ticket", 0)):
                print(f"    running  #{h.get('ticket')}  pid={h.get('pid')}  "
                      f"{_fmt_age(h.get('since'))}  {h.get('label', '')}")
            for i, w in enumerate(sorted(waiting, key=lambda x: x.get("ticket", 0))):
                print(f"    waiting  #{w.get('ticket')}  pid={w.get('pid')}  "
                      f"{_fmt_age(w.get('since'))}  {w.get('label', '')}"
                      f"   (position {i + 1})")
            if not holders and not waiting:
                print("    idle")
        if not any_configured:
            print("")
            print("No QUEUE_SLOTS_* limits are set, so nothing is throttled.")
            print("Set them in .env, e.g.:  QUEUE_SLOTS_TRANSCRIBE=1")
        return 0

    if args.cmd == "reset":
        components = [args.component] if args.component else list(COMPONENTS)
        for name in components:
            queue = Queue(name)
            with queue._locked():
                queue._write({"next_ticket": 1, "holders": [], "waiting": []})
            print(f"reset queue: {name}")
        return 0

    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
