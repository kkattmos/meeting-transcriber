#!/usr/bin/env python3
"""
Tests for lib/slotqueue.py — the machine-wide component queue that makes
several concurrent `./pipeline.sh` sessions take turns.

    python3 lib/test_slotqueue.py

Uses real subprocesses (not threads) for the cross-session cases, because the
whole point is coordination between separate OS processes through files.
"""
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from slotqueue import UNLIMITED, Queue, slots_for  # noqa: E402

SLOTQUEUE = str(Path(__file__).resolve().parent / "slotqueue.py")


class SlotsConfigTest(unittest.TestCase):
    def setUp(self):
        for key in list(os.environ):
            if key.startswith("QUEUE_SLOTS_"):
                del os.environ[key]

    tearDown = setUp

    def test_unset_is_unlimited(self):
        """Nothing is throttled until you configure it — the chosen default."""
        self.assertEqual(slots_for("transcribe"), UNLIMITED)

    def test_component_specific_wins_over_default(self):
        os.environ["QUEUE_SLOTS_DEFAULT"] = "1"
        os.environ["QUEUE_SLOTS_SUMMARIZE"] = "3"
        self.assertEqual(slots_for("summarize"), 3)
        self.assertEqual(slots_for("frames"), 1)

    def test_garbage_is_treated_as_unlimited_not_zero(self):
        os.environ["QUEUE_SLOTS_FRAMES"] = "yes-please"
        self.assertEqual(slots_for("frames"), UNLIMITED)

    def test_empty_string_falls_through_to_default(self):
        os.environ["QUEUE_SLOTS_FRAMES"] = ""
        os.environ["QUEUE_SLOTS_DEFAULT"] = "2"
        self.assertEqual(slots_for("frames"), 2)


class QueueTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def q(self, component="frames"):
        return Queue(component, queue_dir=self.dir)

    def test_unlimited_returns_no_ticket_and_writes_nothing(self):
        ticket = self.q().acquire(pid=os.getpid(), limit=UNLIMITED)
        self.assertIsNone(ticket)
        self.assertFalse((self.dir / "frames.json").exists())

    def test_single_slot_is_granted(self):
        ticket = self.q().acquire(pid=os.getpid(), limit=1)
        self.assertIsNotNone(ticket)
        data = self.q().snapshot()
        self.assertEqual(len(data["holders"]), 1)

    def test_release_frees_the_slot(self):
        queue = self.q()
        first = queue.acquire(pid=os.getpid(), limit=1)
        queue.release(first)
        self.assertEqual(len(queue.snapshot()["holders"]), 0)
        second = queue.acquire(pid=os.getpid(), limit=1)
        self.assertIsNotNone(second)
        self.assertNotEqual(first, second)

    def test_release_is_idempotent(self):
        queue = self.q()
        ticket = queue.acquire(pid=os.getpid(), limit=1)
        queue.release(ticket)
        queue.release(ticket)          # must not raise
        queue.release(None)            # nor on an unlimited no-op ticket
        self.assertEqual(len(queue.snapshot()["holders"]), 0)

    def test_second_caller_waits_until_the_first_releases(self):
        queue = self.q()
        held = queue.acquire(pid=os.getpid(), limit=1)
        got = []

        def waiter():
            t = self.q().acquire(pid=os.getpid(), limit=1, poll=0.05)
            got.append(t)

        thread = threading.Thread(target=waiter)
        thread.start()
        time.sleep(0.3)
        self.assertEqual(got, [], "second caller should still be waiting")

        queue.release(held)
        thread.join(timeout=5)
        self.assertEqual(len(got), 1, "second caller never got the slot")

    def test_fifo_order_is_respected(self):
        """Whoever asked first runs first — the ordering guarantee."""
        queue = self.q()
        held = queue.acquire(pid=os.getpid(), limit=1)
        order = []
        lock = threading.Lock()

        def waiter(name):
            t = self.q().acquire(pid=os.getpid(), limit=1, poll=0.05,
                                 label=name)
            with lock:
                order.append((name, t))

        threads = []
        for name in ("first", "second", "third"):
            th = threading.Thread(target=waiter, args=(name,))
            th.start()
            threads.append(th)
            # Stagger so ticket order is deterministic.
            time.sleep(0.25)

        # Release one at a time; each release should admit exactly the next.
        queue.release(held)
        for _ in range(3):
            time.sleep(0.4)
            snap = self.q().snapshot()
            for holder in snap["holders"]:
                self.q().release(holder["ticket"])
        for th in threads:
            th.join(timeout=5)

        self.assertEqual([name for name, _ in order],
                         ["first", "second", "third"])

    def test_two_slots_allow_two_at_once(self):
        queue = self.q()
        a = queue.acquire(pid=os.getpid(), limit=2)
        b = queue.acquire(pid=os.getpid(), limit=2)
        self.assertIsNotNone(a)
        self.assertIsNotNone(b)
        self.assertEqual(len(queue.snapshot()["holders"]), 2)

    def test_dead_holder_is_reclaimed(self):
        """A SIGKILL'd run must not wedge the queue forever."""
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        queue = self.q()
        queue.acquire(pid=proc.pid, limit=1)
        self.assertEqual(len(queue.snapshot()["holders"]), 1)

        proc.kill()
        proc.wait()

        # The next caller prunes the dead holder and takes the slot.
        ticket = self.q().acquire(pid=os.getpid(), limit=1, poll=0.05)
        self.assertIsNotNone(ticket)
        holders = self.q().snapshot()["holders"]
        self.assertEqual(len(holders), 1)
        self.assertEqual(holders[0]["pid"], os.getpid())

    def test_waiter_gives_up_if_its_own_holder_dies(self):
        """Don't queue on behalf of a process that has already exited."""
        queue = self.q()
        blocker = queue.acquire(pid=os.getpid(), limit=1)

        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        result = {}

        def waiter():
            try:
                self.q().acquire(pid=proc.pid, limit=1, poll=0.05)
                result["outcome"] = "acquired"
            except SystemExit as exc:
                result["outcome"] = str(exc)

        thread = threading.Thread(target=waiter)
        thread.start()
        time.sleep(0.3)
        proc.kill()
        proc.wait()
        thread.join(timeout=5)

        self.assertIn("exited while waiting", result.get("outcome", ""))
        # The abandoned waiter is not left behind in the queue file.
        self.assertEqual(self.q().snapshot()["waiting"], [])
        queue.release(blocker)

    def test_timeout_gives_up_cleanly(self):
        queue = self.q()
        held = queue.acquire(pid=os.getpid(), limit=1)
        with self.assertRaises(SystemExit) as ctx:
            self.q().acquire(pid=os.getpid(), limit=1, poll=0.05, timeout=0.4)
        self.assertIn("timed out", str(ctx.exception))
        self.assertEqual(self.q().snapshot()["waiting"], [])
        queue.release(held)

    def test_components_are_independent(self):
        frames = Queue("frames", queue_dir=self.dir)
        transcribe = Queue("transcribe", queue_dir=self.dir)
        frames.acquire(pid=os.getpid(), limit=1)
        # A busy frames queue must not block transcription.
        ticket = transcribe.acquire(pid=os.getpid(), limit=1, poll=0.05)
        self.assertIsNotNone(ticket)


class CliTest(unittest.TestCase):
    """The surface run_one.sh actually calls."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.env = dict(os.environ)
        self.env["QUEUE_DIR"] = self._tmp.name
        for key in list(self.env):
            if key.startswith("QUEUE_SLOTS_"):
                del self.env[key]

    def tearDown(self):
        self._tmp.cleanup()

    def run_cli(self, *args, env=None, timeout=30):
        return subprocess.run([sys.executable, SLOTQUEUE, *args],
                              capture_output=True, text=True,
                              env=env or self.env, timeout=timeout)

    def test_acquire_prints_empty_ticket_when_unlimited(self):
        out = self.run_cli("acquire", "--component", "frames",
                           "--pid", str(os.getpid()))
        self.assertEqual(out.returncode, 0)
        self.assertEqual(out.stdout.strip(), "")

    def test_acquire_prints_a_ticket_when_limited(self):
        env = dict(self.env, QUEUE_SLOTS_FRAMES="1")
        out = self.run_cli("acquire", "--component", "frames",
                           "--pid", str(os.getpid()), env=env)
        self.assertEqual(out.returncode, 0)
        self.assertTrue(out.stdout.strip().isdigit(), out.stdout)

    def test_run_serializes_two_concurrent_commands(self):
        """The cross-session case, with real separate processes."""
        env = dict(self.env, QUEUE_SLOTS_FRAMES="1", QUEUE_POLL_SECONDS="0.1")
        marker = Path(self._tmp.name) / "trace.txt"
        script = (
            "import sys,time;"
            "open(sys.argv[1],'a').write(f'start {sys.argv[2]}\\n');"
            "time.sleep(0.6);"
            "open(sys.argv[1],'a').write(f'end {sys.argv[2]}\\n')"
        )
        procs = [
            subprocess.Popen(
                [sys.executable, SLOTQUEUE, "run", "--component", "frames",
                 "--", sys.executable, "-c", script, str(marker), name],
                env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            for name in ("A", "B")
        ]
        for p in procs:
            p.wait(timeout=30)

        lines = marker.read_text().split()
        # Interleaved would be start,start,end,end. Serialized means each
        # command's start is immediately followed by its own end.
        events = marker.read_text().strip().splitlines()
        self.assertEqual(len(events), 4, events)
        self.assertTrue(events[0].startswith("start"), events)
        self.assertTrue(events[1].startswith("end"), events)
        self.assertEqual(events[0].split()[1], events[1].split()[1], events)
        self.assertEqual(events[2].split()[1], events[3].split()[1], events)
        del lines

    def test_run_allows_concurrency_when_unlimited(self):
        env = dict(self.env, QUEUE_POLL_SECONDS="0.1")
        marker = Path(self._tmp.name) / "trace2.txt"
        script = (
            "import sys,time;"
            "open(sys.argv[1],'a').write(f'start {sys.argv[2]}\\n');"
            "time.sleep(0.6);"
            "open(sys.argv[1],'a').write(f'end {sys.argv[2]}\\n')"
        )
        procs = [
            subprocess.Popen(
                [sys.executable, SLOTQUEUE, "run", "--component", "frames",
                 "--", sys.executable, "-c", script, str(marker), name],
                env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            for name in ("A", "B")
        ]
        for p in procs:
            p.wait(timeout=30)
        events = marker.read_text().strip().splitlines()
        # Unlimited: both start before either finishes.
        self.assertTrue(events[0].startswith("start"))
        self.assertTrue(events[1].startswith("start"))

    def test_run_propagates_exit_code(self):
        env = dict(self.env, QUEUE_SLOTS_FRAMES="1")
        out = self.run_cli("run", "--component", "frames", "--",
                           sys.executable, "-c", "raise SystemExit(7)", env=env)
        self.assertEqual(out.returncode, 7)

    def test_run_releases_the_slot_even_when_the_command_fails(self):
        env = dict(self.env, QUEUE_SLOTS_FRAMES="1")
        self.run_cli("run", "--component", "frames", "--",
                     sys.executable, "-c", "raise SystemExit(1)", env=env)
        out = self.run_cli("status", "--component", "frames", env=env)
        self.assertNotIn("running", out.stdout)

    def test_status_reports_holders(self):
        env = dict(self.env, QUEUE_SLOTS_FRAMES="1")
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        try:
            self.run_cli("acquire", "--component", "frames",
                         "--pid", str(proc.pid), "--label", "my_run", env=env)
            out = self.run_cli("status", env=env)
            self.assertIn("running", out.stdout)
            self.assertIn("my_run", out.stdout)
        finally:
            proc.kill()
            proc.wait()

    def test_reset_clears_a_stuck_queue(self):
        env = dict(self.env, QUEUE_SLOTS_FRAMES="1")
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        try:
            self.run_cli("acquire", "--component", "frames",
                         "--pid", str(proc.pid), env=env)
            self.run_cli("reset", "--component", "frames", env=env)
            out = self.run_cli("status", "--component", "frames", env=env)
            self.assertIn("idle", out.stdout)
        finally:
            proc.kill()
            proc.wait()


if __name__ == "__main__":
    unittest.main(verbosity=2)
