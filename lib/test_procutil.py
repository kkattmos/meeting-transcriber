#!/usr/bin/env python3
"""lib/procutil.py and lib/platform.sh: the process/lock helpers shared by
Linux and the Windows branch. On Linux they must behave exactly like the
os.kill / fcntl / kill -0 / setsid code they replaced.

    python3 lib/test_procutil.py
"""
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import procutil  # noqa: E402

PLATFORM_SH = HERE / "platform.sh"


def sh(script, **env):
    return subprocess.run(["bash", "-c", f'. "{PLATFORM_SH}"; {script}'],
                          capture_output=True, text=True,
                          env={**os.environ, **env})


class PidAliveTest(unittest.TestCase):
    def test_self_is_alive_and_a_reaped_child_is_not(self):
        self.assertTrue(procutil.pid_alive(os.getpid()))
        p = subprocess.Popen(["true"])
        p.wait()
        self.assertFalse(procutil.pid_alive(p.pid))

    def test_garbage_is_not_alive(self):
        for value in (None, "", "abc", 0, -5):
            with self.subTest(value=value):
                self.assertFalse(procutil.pid_alive(value))

    def test_windows_never_calls_os_kill(self):
        # On Windows os.kill(pid, 0) would SEND CTRL_C; the Win32 path is used.
        with mock.patch.object(procutil, "IS_WINDOWS", True), \
                mock.patch.object(procutil, "_win_pid_alive", return_value=True) as win, \
                mock.patch("os.kill", side_effect=AssertionError("os.kill on Windows")):
            self.assertTrue(procutil.pid_alive(1234))
        win.assert_called_once_with(1234)

    def test_cli(self):
        r = subprocess.run([sys.executable, str(HERE / "procutil.py"), "alive", str(os.getpid())])
        self.assertEqual(r.returncode, 0)
        r = subprocess.run([sys.executable, str(HERE / "procutil.py"), "alive", "x"])
        self.assertEqual(r.returncode, 1)


class FileLockTest(unittest.TestCase):
    def test_a_second_process_waits_for_the_lock(self):
        with tempfile.TemporaryDirectory() as tmp:
            lock = Path(tmp) / "l"
            out = Path(tmp) / "order"
            child = (f"import sys, time; sys.path.insert(0, {str(HERE)!r}); import procutil\n"
                     f"fh = open({str(lock)!r}, 'a+'); procutil.lock_file(fh)\n"
                     f"open({str(out)!r}, 'a').write('child\\n')")
            with open(lock, "a+") as fh:
                procutil.lock_file(fh)
                p = subprocess.Popen([sys.executable, "-c", child])
                time.sleep(0.5)
                self.assertFalse(out.exists(), "the child got the lock while we held it")
                out.write_text("parent\n")
                procutil.unlock_file(fh)
            p.wait(timeout=10)
            self.assertEqual(out.read_text(), "parent\nchild\n")


class PlatformShTest(unittest.TestCase):
    def test_linux_is_not_windows(self):
        self.assertEqual(sh("mb_is_windows && echo yes || echo no").stdout.strip(), "no")

    def test_mb_pid_is_the_shell_pid_on_linux(self):
        r = sh('echo "$(mb_pid) $$"')
        a, b = r.stdout.split()
        self.assertEqual(a, b)
        self.assertEqual(sh("mb_pid 4242").stdout.strip(), "4242")

    def test_mb_alive(self):
        self.assertEqual(sh(f"mb_alive {os.getpid()} && echo up").stdout.strip(), "up")
        p = subprocess.Popen(["true"]); p.wait()
        self.assertEqual(sh(f"mb_alive {p.pid} || echo down").stdout.strip(), "down")
        self.assertEqual(sh('mb_alive "" || echo down').stdout.strip(), "down")

    def test_mb_tree_and_kill_tree(self):
        r = sh('bash -c "sleep 30 & sleep 30 & wait" & parent=$!; sleep 0.5; '
               'mb_tree $parent | wc -l; mb_kill_tree TERM $parent; sleep 0.3; '
               'mb_alive $parent && echo still || echo gone')
        lines = r.stdout.split()
        self.assertEqual(lines[0], "3")      # bash + two sleeps
        self.assertEqual(lines[-1], "gone")

    def test_mb_detach_survives_and_logs(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "log"
            r = sh(f'pid=$(mb_detach "{log}" bash -c "echo hello; sleep 1"); echo $pid')
            pid = int(r.stdout.strip())
            for _ in range(40):
                if "hello" in (log.read_text() if log.exists() else ""):
                    break
                time.sleep(0.05)
            self.assertIn("hello", log.read_text())
            # In its own session on Linux (setsid), like before.
            try:
                self.assertNotEqual(os.getsid(pid), os.getsid(0))
            except ProcessLookupError:
                pass


if __name__ == "__main__":
    unittest.main(verbosity=2)
