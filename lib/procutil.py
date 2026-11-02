"""Process liveness and file locks that work on Linux and native Windows.

Stdlib only. On Linux these are exactly what the code did before
(`os.kill(pid, 0)`, `fcntl.flock`); on Windows (the `windows` branch, where
the bash scripts run under Git Bash) they use the Win32 API instead:

* `os.kill(pid, 0)` is NOT a liveness test on Windows — signal 0 is
  CTRL_C_EVENT there, and os.kill would send it. `pid_alive` asks
  OpenProcess/GetExitCodeProcess instead.
* `fcntl` does not exist on Windows; `lock_file` / `unlock_file` use
  `msvcrt.locking` on the first byte (a blocking, exclusive lock).

The pids are always *Windows* pids on Windows: lib/platform.sh makes the
bash side write `/proc/<pid>/winpid`, never Git Bash's own MSYS pid, so a pid
written by a script and one read by Python mean the same process.
"""
import os
import sys

IS_WINDOWS = os.name == "nt"


def pid_alive(pid):
    """Is a process with this pid running? (A zombie counts as alive here;
    callers that care, like screen/browser.py, check for that themselves.)"""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    if IS_WINDOWS:
        return _win_pid_alive(pid)
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # it exists; it just isn't ours
    except OSError:
        return False


def _win_pid_alive(pid):
    import ctypes
    from ctypes import wintypes
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    STILL_ACTIVE = 259
    kernel32.OpenProcess.restype = wintypes.HANDLE
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        # ERROR_ACCESS_DENIED: it exists, owned by someone else.
        return ctypes.get_last_error() == 5
    try:
        code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return False
        return code.value == STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)


def lock_file(fh):
    """Block until this process holds an exclusive lock on the open file."""
    if IS_WINDOWS:
        import msvcrt
        import time
        fh.seek(0)
        while True:
            try:
                msvcrt.locking(fh.fileno(), msvcrt.LK_LOCK, 1)
                return
            except OSError:
                # LK_LOCK gives up after ~10s of retries; keep waiting, like flock.
                time.sleep(0.05)
    import fcntl
    fcntl.flock(fh.fileno(), fcntl.LOCK_EX)


def unlock_file(fh):
    if IS_WINDOWS:
        import msvcrt
        fh.seek(0)
        try:
            msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
        return
    import fcntl
    fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


def main(argv):
    """`procutil.py alive PID` exits 0 when it is running (for scripts)."""
    if len(argv) == 3 and argv[1] == "alive":
        return 0 if pid_alive(argv[2]) else 1
    print("usage: procutil.py alive PID", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
