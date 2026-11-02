#!/usr/bin/env python3
"""Stage 1 on native Windows: join with capture.py, record a VISIBLE window.

The Linux recorder (record_screen.sh) hides the bot's browser on its own Xvfb
display and gives it its own PulseAudio sink. Windows has neither, so — as
settled with the operator 2026-11-02 — this records what Windows can offer:

* **Picture**: the bot's browser runs as a normal full-screen (kiosk) window,
  on the monitor MEETING_BOT_KIOSK_MONITOR names (0-based; give the bot a
  second monitor, or a dummy-HDMI plug). ffmpeg's gdigrab records THAT window
  by its title — found from the window handles of capture.py's own process
  tree, so another Firefox or Chrome on the desktop is never picked. The
  window must stay uncovered and must not be minimised while it records.
* **Sound**: the bot's browser plays into the free VB-Audio Virtual Cable
  ("CABLE Input", chosen once for the bot's browser in Windows' per-app sound
  settings — setup.ps1 installs Firefox ESR in its own folder so that setting
  never touches your own Firefox), and ffmpeg records "CABLE Output" with
  dshow. Only the meeting is recorded, not the PC's other sound.
* **One recording at a time**: there is one cable, so a second meeting would
  mix into the first. A second recorder refuses to start.
* **Stopping**: Windows cannot send another process SIGINT, so ffmpeg is
  started with a pipe on stdin and stopped with its own "q" command, which
  finalises the MP4. kill_meeting.sh touches the run's `kill` sentinel
  (capture.py leaves the call), and past its grace period taskkills
  capture.py's tree; either way this script then stops ffmpeg itself.

Same arguments and contract as screen/record_screen.sh (run_one.sh calls it
through screen/record_windows.sh, MEETING_BOT_RECORDER):

    record_windows.py <meeting_url> <meeting_name> <display_name> <output>

Environment: MEETING_BOT_RUN_DIR (sentinels, record.pid), RECORD_MEDIA
(video|audio), RECORD_GEOMETRY (the video is scaled to it), RECORD_FRAMERATE,
WINDOWS_AUDIO_DEVICE (default "CABLE Output (VB-Audio Virtual Cable)"),
ADMIT_WAIT_SECONDS, FFMPEG_BIN / FFPROBE_BIN.
"""
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT / "lib"))
from procutil import pid_alive  # noqa: E402

DEFAULT_AUDIO_DEVICE = "CABLE Output (VB-Audio Virtual Cable)"
FINALISE_SECONDS = int(os.environ.get("KILL_FINALISE_SECONDS", "120"))


# --- pure helpers (tested on any OS) ----------------------------------------

def parse_dshow_audio_devices(text):
    """Audio device names from `ffmpeg -list_devices true -f dshow -i dummy`.

    Newer ffmpeg prints `"name" (audio)` per line; older ones print a
    `DirectShow audio devices` header followed by `"name"` lines."""
    names, in_audio = [], False
    for line in text.splitlines():
        if "DirectShow audio devices" in line:
            in_audio = True
            continue
        if "DirectShow video devices" in line:
            in_audio = False
            continue
        if "Alternative name" in line:
            continue
        m = re.search(r'"([^"]+)"\s*(\((audio|video)\))?', line)
        if not m:
            continue
        kind = m.group(3)
        if kind == "audio" or (kind is None and in_audio):
            names.append(m.group(1))
    return names


def pick_audio_device(available, wanted):
    """The configured device if present; else a VB-CABLE output by its usual
    name; else None."""
    if wanted in available:
        return wanted
    for name in available:
        if name.lower().startswith("cable output"):
            return name
    return None


def ffmpeg_command(ffmpeg, out, media, audio_device, window_title=None,
                   geometry="1920x1080", framerate="15"):
    """The ffmpeg command line. The encoder settings are the Linux recorder's
    (CLAUDE.md: libx264 ultrafast crf 28, AAC 128k)."""
    audio_in = ["-f", "dshow", "-i", f"audio={audio_device}"]
    if media == "audio":
        return [ffmpeg, "-y", "-hide_banner", *audio_in,
                "-c:a", "aac", "-b:a", "128k", str(out)]
    w, h = geometry.lower().split("x")
    scale = (f"scale={w}:{h}:force_original_aspect_ratio=decrease,"
             f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,setsar=1")
    return [ffmpeg, "-y", "-hide_banner",
            "-f", "gdigrab", "-framerate", str(framerate), "-draw_mouse", "0",
            "-i", f"title={window_title}",
            *audio_in,
            "-vf", scale,
            "-c:v", "libx264", "-preset", "ultrafast", "-crf", "28",
            "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "128k",
            "-shortest", str(out)]


def write_pid_file(path, **fields):
    """record.pid in record_screen.sh's key=value format (kill_meeting.sh
    reads record / join / ffmpeg). Pids are Windows pids."""
    Path(path).write_text("".join(f"{k}={v}\n" for k, v in fields.items() if v is not None))


# --- Windows only -------------------------------------------------------------

def _descendants(root_pid):
    """root_pid and every process below it (Toolhelp snapshot)."""
    import ctypes
    from ctypes import wintypes

    class PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                    ("th32ProcessID", wintypes.DWORD),
                    ("th32DefaultHeapID", ctypes.c_void_p),
                    ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
                    ("th32ParentProcessID", wintypes.DWORD),
                    ("pcPriClassBase", ctypes.c_long), ("dwFlags", wintypes.DWORD),
                    ("szExeFile", ctypes.c_wchar * 260)]
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    snap = k32.CreateToolhelp32Snapshot(0x2, 0)  # TH32CS_SNAPPROCESS
    parent_of = {}
    entry = PROCESSENTRY32W()
    entry.dwSize = ctypes.sizeof(entry)
    ok = k32.Process32FirstW(snap, ctypes.byref(entry))
    while ok:
        parent_of[entry.th32ProcessID] = entry.th32ParentProcessID
        ok = k32.Process32NextW(snap, ctypes.byref(entry))
    k32.CloseHandle(snap)
    return descendants_of(root_pid, parent_of)


def descendants_of(root_pid, parent_of):
    """Pure: the pids under root_pid, given {pid: parent pid}."""
    tree, changed = {root_pid}, True
    while changed:
        changed = False
        for pid, parent in parent_of.items():
            if parent in tree and pid not in tree:
                tree.add(pid)
                changed = True
    return tree


def find_browser_window(root_pid):
    """(hwnd, title) of the largest visible top-level window owned by a
    process in root_pid's tree, or (None, None)."""
    import ctypes
    from ctypes import wintypes
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    tree = _descendants(root_pid)
    best = [None, None, 0]
    proto = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def visit(hwnd, _):
        if not user32.IsWindowVisible(hwnd):
            return True
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value not in tree:
            return True
        n = user32.GetWindowTextLengthW(hwnd)
        if n == 0:
            return True
        buf = ctypes.create_unicode_buffer(n + 1)
        user32.GetWindowTextW(hwnd, buf, n + 1)
        rect = wintypes.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(rect))
        area = (rect.right - rect.left) * (rect.bottom - rect.top)
        if area > best[2]:
            best[:] = [hwnd, buf.value, area]
        return True

    user32.EnumWindows(proto(visit), 0)
    return best[0], best[1]


def list_audio_devices(ffmpeg):
    r = subprocess.run([ffmpeg, "-hide_banner", "-list_devices", "true", "-f", "dshow",
                        "-i", "dummy"], capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    return parse_dshow_audio_devices(r.stdout + r.stderr)


# --- the recorder -----------------------------------------------------------------

def main(argv):
    if len(argv) < 2:
        print("Usage: record_windows.py <meeting_url> [meeting_name] [display_name] [output]",
              file=sys.stderr)
        return 1
    url = argv[1]
    display_name = argv[3] if len(argv) > 3 else "Meeting Bot"
    media = os.environ.get("RECORD_MEDIA", "video")
    ext = "m4a" if media == "audio" else "mp4"
    out = Path(argv[4]) if len(argv) > 4 else Path(f"{argv[2] if len(argv) > 2 else 'meeting'}.{ext}")
    out.parent.mkdir(parents=True, exist_ok=True)
    ffmpeg = os.environ.get("FFMPEG_BIN", "ffmpeg")
    ffprobe = os.environ.get("FFPROBE_BIN", "ffprobe")
    run_dir = Path(os.environ.get("MEETING_BOT_RUN_DIR") or out.parent)
    kill_sentinel, admitted = run_dir / "kill", run_dir / "admitted"
    pid_file = run_dir / "record.pid"
    admit_limit = int(os.environ.get("ADMIT_WAIT_SECONDS", "620"))
    geometry = os.environ.get("RECORD_GEOMETRY", "1920x1080")
    framerate = os.environ.get("RECORD_FRAMERATE", "15")

    # One cable, one recording.
    bot_root = Path(os.environ.get("MEETING_BOT_ROOT", run_dir.parent.parent))
    lock = bot_root / "state" / "windows-recorder.pid"
    lock.parent.mkdir(parents=True, exist_ok=True)
    try:
        other = int(lock.read_text().strip())
    except (OSError, ValueError):
        other = None
    if other and other != os.getpid() and pid_alive(other):
        print(f"ERROR: another meeting is being recorded (pid {other}). On Windows there "
              "is one VB-CABLE, so meetings are recorded one at a time.", file=sys.stderr)
        return 1
    lock.write_text(str(os.getpid()))

    wanted = os.environ.get("WINDOWS_AUDIO_DEVICE", DEFAULT_AUDIO_DEVICE)
    device = pick_audio_device(list_audio_devices(ffmpeg), wanted)
    if not device:
        print(f"ERROR: the audio device {wanted!r} was not found. Install VB-Audio Virtual "
              "Cable (README: \"Windows\") or set WINDOWS_AUDIO_DEVICE to the device ffmpeg "
              "lists with:  ffmpeg -list_devices true -f dshow -i dummy", file=sys.stderr)
        lock.unlink(missing_ok=True)
        return 1

    stopping = {"asked": False}

    def ask_to_leave(*_):
        if not stopping["asked"]:
            stopping["asked"] = True
            print("==> Stop requested — asking the bot to leave the meeting.", flush=True)
            kill_sentinel.touch()
    for sig in (signal.SIGINT, signal.SIGTERM, getattr(signal, "SIGBREAK", None)):
        if sig is not None:
            try:
                signal.signal(sig, ask_to_leave)
            except (OSError, ValueError):
                pass

    admitted.unlink(missing_ok=True)
    env = dict(os.environ, RECORD_GEOMETRY=geometry)
    join = subprocess.Popen([sys.executable, "-u", str(HERE / "capture.py"), url, display_name],
                            env=env)
    write_pid_file(pid_file, record=os.getpid(), join=join.pid, display="window", sink=device)
    rc = 1
    ff = None
    try:
        print("==> Waiting for admission before starting the recorder...", flush=True)
        waited = 0
        while not admitted.exists():
            if join.poll() is not None:
                print("Join script exited before admission (join failed, or not admitted).",
                      file=sys.stderr)
                return 1
            if waited >= admit_limit:
                print("Timed out waiting for admission — stopping.", file=sys.stderr)
                kill_sentinel.touch()
                join.wait(timeout=60)
                return 1
            time.sleep(2)
            waited += 2

        title = None
        if media != "audio":
            for _ in range(15):
                _hwnd, title = find_browser_window(join.pid)
                if title:
                    break
                time.sleep(1)
            if not title:
                print("ERROR: could not find the bot's browser window to record.", file=sys.stderr)
                kill_sentinel.touch()
                return 1
            print(f"==> Admitted. Recording the window {title!r} + {device} -> {out}", flush=True)
        else:
            print(f"==> Admitted. Recording audio only ({device}) -> {out}", flush=True)
        log = open(f"{out.with_suffix('')}_ffmpeg.log", "w", encoding="utf-8")
        ff = subprocess.Popen(ffmpeg_command(ffmpeg, out, media, device, title, geometry, framerate),
                              stdin=subprocess.PIPE, stdout=log, stderr=subprocess.STDOUT)
        write_pid_file(pid_file, record=os.getpid(), join=join.pid, ffmpeg=ff.pid,
                       display="window", sink=device)

        print("==> Recording. Waiting for the meeting to end...", flush=True)
        while join.poll() is None:
            if ff.poll() is not None:
                print("ERROR: ffmpeg stopped while the meeting was still on — see "
                      f"{log.name}. Asking the bot to leave.", file=sys.stderr)
                kill_sentinel.touch()
                join.wait()
                break
            time.sleep(2)
        rc = 0
    finally:
        if ff is not None and ff.poll() is None:
            # "q" is ffmpeg's own clean stop: it writes the index and closes the file.
            try:
                ff.stdin.write(b"q")
                ff.stdin.flush()
                ff.stdin.close()
            except OSError:
                pass
            try:
                ff.wait(timeout=FINALISE_SECONDS)
            except subprocess.TimeoutExpired:
                print("WARNING: ffmpeg did not finish finalising; stopping it.", file=sys.stderr)
                ff.terminate()
        if join.poll() is None:
            join.wait()
        pid_file.unlink(missing_ok=True)
        lock.unlink(missing_ok=True)

    if ff is None:
        return 1
    probe = subprocess.run([ffprobe, "-v", "error", "-show_entries", "format=duration",
                            "-of", "csv=p=0", str(out)], capture_output=True, text=True)
    if probe.returncode != 0 or not probe.stdout.strip():
        print(f"ERROR: the recording {out} is not readable (see the ffmpeg log).", file=sys.stderr)
        return 1
    print(f"==> Recording saved: {out} ({float(probe.stdout.strip() or 0):.0f}s)")
    return rc


if __name__ == "__main__":
    sys.exit(main(sys.argv))
