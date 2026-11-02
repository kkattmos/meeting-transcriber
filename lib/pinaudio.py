#!/usr/bin/env python3
"""Keep a recording's browser audio on the recording's own sink.

    python3 lib/pinaudio.py <root_pid> <sink_name>

Moves every PulseAudio/PipeWire playback stream that belongs to <root_pid>
or any of its descendants (the browser driver -> geckodriver -> Firefox) and
is not already on <sink_name> onto it. Prints one line per move; silent when
nothing needed moving. Exit 0 always — this is a best-effort guard run every
few seconds by record_screen.sh, and a failure must never stop a recording.

Why it exists (found live 2026-09-29): PULSE_SINK is only a request.
WirePlumber remembers routing per application name, and the bot's browser and
the operator's own browser were both "Firefox" — a stream the operator moved
in pavucontrol was "restored" onto the bot's streams too, and the bot sent the
meeting's audio to the wrong device and recorded silence. record_screen.sh
now also renames the bot's client (PULSE_PROP_OVERRIDE application.name
"Meeting Bot"), which keeps the two apart; this pins it regardless.
"""
import json
import os
import subprocess
import sys


def descendants(root):
    """root and every process below it, from /proc."""
    children = {}
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            with open(f"/proc/{entry}/stat") as fh:
                ppid = int(fh.read().rsplit(")", 1)[1].split()[1])
        except (OSError, IndexError, ValueError):
            continue
        children.setdefault(ppid, []).append(int(entry))
    out, stack = set(), [root]
    while stack:
        pid = stack.pop()
        if pid in out:
            continue
        out.add(pid)
        stack.extend(children.get(pid, []))
    return out


def main(argv):
    if len(argv) != 2:
        print(__doc__.split("\n\n")[1].strip(), file=sys.stderr)
        return 0
    try:
        root = int(argv[0])
    except ValueError:
        return 0
    sink_name = argv[1]
    try:
        sinks = json.loads(subprocess.run(
            ["pactl", "-f", "json", "list", "short", "sinks"],
            capture_output=True, text=True, timeout=10).stdout or "[]")
        inputs = json.loads(subprocess.run(
            ["pactl", "-f", "json", "list", "sink-inputs"],
            capture_output=True, text=True, timeout=10).stdout or "[]")
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return 0
    target = next((s.get("index") for s in sinks if s.get("name") == sink_name), None)
    if target is None:
        return 0
    ours = descendants(root)
    for stream in inputs:
        props = stream.get("properties", {})
        try:
            pid = int(props.get("application.process.id", "-1"))
        except ValueError:
            continue
        if pid in ours and stream.get("sink") != target:
            subprocess.run(["pactl", "move-sink-input", str(stream["index"]), sink_name],
                           capture_output=True, timeout=10)
            print(f"==> Moved the bot's audio stream #{stream['index']} back onto "
                  f"{sink_name} (it had been routed to sink #{stream.get('sink')}).")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
