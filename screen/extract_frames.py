#!/usr/bin/env python3
"""
Extract frames from a meeting recording for the AI summary agent.

Two passes:
  1. Scene-change detection: ffmpeg emits a frame whenever the scene-change
     score crosses SCENE_THRESHOLD (default 0.3). Catches slide transitions,
     shared-screen cuts, and changes inside shared video streams.
  2. Periodic safety net: emit one frame every FRAME_PERIOD_SECONDS (default 30)
     even when nothing is changing. Covers "static slide for 5 minutes" cases
     and fills gaps if scene-change briefly dips during a shared video.

Writes a manifest.json listing each frame's path, timestamp, and kind
("scene_change" or "periodic"). The summary agent reads this manifest to know
which frames to send to the LLM and when each one occurred.

CLI:
  python3 screen/extract_frames.py <video_path> <output_dir> [<meeting_name>]

Environment:
  SCENE_THRESHOLD       default 0.3   (ffmpeg scene score, 0.0-1.0)
  FRAME_PERIOD_SECONDS  default 30    (0 disables the periodic safety net)
  FRAME_OUTPUT_DIR      default /opt/meeting-bot/frames
"""
import json
import os
import re
import subprocess
import sys
from pathlib import Path

DEFAULT_SCENE_THRESHOLD = 0.3
DEFAULT_PERIOD_SECONDS = 30
DEFAULT_OUTPUT_ROOT = "/opt/meeting-bot/frames"


def parse_showinfo(stderr_text):
    """Parse ffmpeg `-showinfo` lines from stderr into (pts_time, frame_type).

    Lines look like:
      [Parsed_showinfo_1 @ ...] n:0 pts:12345 pts_time:12.345 pos:... ...
                                  key_frame:1 type:I [FRAME]
    """
    out = []
    for line in stderr_text.splitlines():
        m = re.search(r"pts_time:([\d.]+)", line)
        if not m:
            continue
        try:
            ts = float(m.group(1))
        except ValueError:
            continue
        out.append(ts)
    return out


def extract_scene_change_frames(video, out_dir, threshold):
    """Run ffmpeg with the scene filter. Returns list of timestamps (seconds)."""
    # -vsync vfr keeps variable frame rate (we only get frames the filter emits).
    # showinfo writes one line per emitted frame to stderr so we can recover
    # the original PTS without depending on filenames.
    cmd = [
        "ffmpeg", "-nostdin", "-loglevel", "info",
        "-i", video,
        "-vf", f"select='gt(scene,{threshold})',showinfo",
        "-vsync", "vfr",
        "-frame_pts", "true",
        str(out_dir / "scene_%05d.jpg"),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0 and not proc.stderr:
        raise RuntimeError(f"ffmpeg scene-change pass failed: {proc.stderr}")
    return parse_showinfo(proc.stderr)


def extract_periodic_frames(video, out_dir, period, video_duration):
    """Run ffmpeg to grab one frame every N seconds. Returns list of timestamps."""
    if period <= 0 or video_duration <= 0:
        return []
    # fps=1/period gives one frame per period seconds.
    cmd = [
        "ffmpeg", "-nostdin", "-loglevel", "info",
        "-i", video,
        "-vf", f"fps=1/{period},showinfo",
        "-vsync", "vfr",
        str(out_dir / "periodic_%05d.jpg"),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0 and not proc.stderr:
        raise RuntimeError(f"ffmpeg periodic pass failed: {proc.stderr}")
    return parse_showinfo(proc.stderr)


def probe_duration(video):
    """ffprobe the video duration in seconds. Returns 0.0 on failure."""
    try:
        out = subprocess.check_output(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", video],
            text=True,
        ).strip()
        return float(out)
    except (subprocess.CalledProcessError, ValueError):
        return 0.0


def main():
    if len(sys.argv) < 3:
        print(f"Usage: {sys.argv[0]} <video_path> <output_dir> [meeting_name]")
        sys.exit(1)

    video = sys.argv[1]
    out_dir_arg = sys.argv[2]
    meeting_name = sys.argv[3] if len(sys.argv) > 3 else "meeting"

    threshold = float(os.environ.get("SCENE_THRESHOLD", DEFAULT_SCENE_THRESHOLD))
    period = int(os.environ.get("FRAME_PERIOD_SECONDS", DEFAULT_PERIOD_SECONDS))
    output_root = os.environ.get("FRAME_OUTPUT_DIR", DEFAULT_OUTPUT_ROOT)

    out_dir = Path(out_dir_arg)
    if out_dir.exists() and any(out_dir.iterdir()):
        print(f"WARNING: output dir {out_dir} already has files - reusing.")
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"==> Probing video duration")
    duration = probe_duration(video)
    print(f"    duration = {duration:.1f}s")

    print(f"==> Scene-change pass (threshold={threshold})")
    scene_timestamps = extract_scene_change_frames(video, out_dir, threshold)
    print(f"    emitted {len(scene_timestamps)} scene-change frames")

    print(f"==> Periodic pass (every {period}s)")
    periodic_timestamps = extract_periodic_frames(video, out_dir, period, duration)
    print(f"    emitted {len(periodic_timestamps)} periodic frames")

    # Build a deduplicated manifest. The two passes can produce frames very
    # close together (e.g. a scene change at the same moment as a periodic
    # tick); we keep the scene_change one and skip the duplicate periodic.
    half_period = period / 2 if period > 0 else 0
    kept = []  # (timestamp, kind, path)

    def path_for(prefix, idx):
        return str(out_dir / f"{prefix}_{idx:05d}.jpg")

    # First, emit scene_change frames.
    for i, ts in enumerate(scene_timestamps, start=1):
        kept.append((ts, "scene_change", path_for("scene", i)))

    # Then, emit periodic frames only if no scene_change is within ±half_period.
    scene_ts_set = [k[0] for k in kept]
    for i, ts in enumerate(periodic_timestamps, start=1):
        if any(abs(ts - s) <= half_period for s in scene_ts_set):
            continue
        kept.append((ts, "periodic", path_for("periodic", i)))

    # Sort by timestamp.
    kept.sort(key=lambda x: x[0])

    manifest = {
        "video": str(video),
        "meeting_name": meeting_name,
        "duration_seconds": duration,
        "scene_threshold": threshold,
        "frame_period_seconds": period if period > 0 else None,
        "frame_count": len(kept),
        "frames": [
            {"timestamp_s": round(ts, 3), "kind": kind, "path": path}
            for (ts, kind, path) in kept
        ],
    }

    manifest_path = out_dir / "manifest.json"
    with open(manifest_path, "w") as f:
        json.dump(manifest, f, indent=2)

    print(f"==> Wrote manifest with {len(kept)} frames -> {manifest_path}")


if __name__ == "__main__":
    main()