#!/usr/bin/env python3
"""
Option 3: Summarize a meeting by sending the transcript + extracted frames
to a vision-capable LLM.

This is the standalone entry point. When pipeline.sh is used, it's called
automatically after Option 1 (record) and Option 2 (transcribe).

Usage:
    python3 summarize/summarize.py <video_or_youtube_url> <transcript_path> [<output_md_path>] [--prompt NAME]

--prompt NAME picks which file in prompts/ to use (e.g. --prompt standup
loads prompts/standup.md). Can also be set via the SUMMARY_PROMPT env var;
the flag takes precedence over the env var. Defaults to prompts/summarize.md
if neither is given. NAME can be given with or without the .md suffix.

The <video_or_youtube_url> argument can be either:
  - A local file path (MP4 from screen/record_screen.sh, or any container
    ffmpeg can read).
  - A YouTube URL (youtube.com/watch?v= or youtu.be/). The video is
    downloaded via yt-dlp before frame extraction.

If <output_md_path> is omitted, writes to
/opt/meeting-bot/summaries/<derived-name>_<timestamp>.md.

Configuration (env vars):
  SCENE_THRESHOLD        default 0.3   (passed to extract_frames.py)
  FRAME_PERIOD_SECONDS   default 30    (set to 0 to disable periodic pass)
  FRAME_OUTPUT_DIR       default /opt/meeting-bot/frames
  SUMMARY_BACKEND        "gemini" (default), "anthropic", "nvidia_nim",
                         "ollama", or "fallback" — see summarize/llm_client.py
  GOOGLE_API_KEY / GEMINI_API_KEY   required when backend=gemini
  GEMINI_MODEL           default gemini-2.5-flash
  ANTHROPIC_BASE_URL     default https://api.anthropic.com
  ANTHROPIC_API_KEY      required when backend=anthropic/fcc
  SUMMARY_MODEL          default claude-sonnet-4-5
  OLLAMA_HOST            default http://localhost:11434
  OLLAMA_MODEL           default llava:13b (only if backend=ollama)
  SUMMARY_PROMPT         name of file in prompts/ to use (no .md needed);
                         overridden by --prompt; default: summarize.md
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path

# Allow running this script directly (./summarize/summarize.py) without
# needing the summarize/ dir on PYTHONPATH.
SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))


def _load_dotenv():
    """Best-effort loader for the repo-root `.env` file.

    Matches the bash `source_env.sh` semantics: skip blanks / comments,
    strip one layer of surrounding quotes, do NOT override values already
    in os.environ. Used when summarize.py is invoked directly (not via
    pipeline.sh, which pre-loads the env through the bash loader).
    """
    import re as _re
    root = SCRIPT_DIR.parent
    candidates = [root / ".env", Path.cwd() / ".env"]
    for path in candidates:
        if not path.is_file():
            continue
        try:
            for line in path.read_text().splitlines():
                stripped = line.strip()
                if not stripped or stripped.startswith("#"):
                    continue
                m = _re.match(r'^([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$', stripped)
                if not m:
                    continue
                key, val = m.group(1), m.group(2)
                # Strip a single layer of surrounding " or '.
                if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
                    val = val[1:-1]
                if key not in os.environ:
                    os.environ[key] = val
        except OSError:
            # Don't blow up just because the .env file is unreadable;
            # whatever is already in os.environ still works.
            pass
        return  # first .env file wins; don't cascade


_load_dotenv()

from llm_client import FrameMeta, summarize, summarize_with_fallback  # noqa: E402

ROOT_DIR = SCRIPT_DIR.parent
SCREEN_DIR = ROOT_DIR / "screen"

PROMPTS_DIR = SCRIPT_DIR / "prompts"
PROMPT_PATH = PROMPTS_DIR / "summarize.md"
DEFAULT_SUMMARY_DIR = "/opt/meeting-bot/summaries"
DEFAULT_FRAME_DIR = "/opt/meeting-bot/frames"
# YouTube downloads go here instead of the default /tmp because a 4-vCPU
# server-side /tmp (often a small tmpfs) can fill up and starve the rest of
# the system. /opt has room; the dir is created on demand by the caller.
YT_TMP_ROOT = Path("/opt/meeting-bot/tmp")
# Stale-dir sweep threshold: anything left over from a crashed prior run
# older than this is removed at startup so a leak doesn't accumulate.
YT_STALE_SECONDS = 24 * 3600

YOUTUBE_URL_RE = re.compile(r"(youtube\.com/watch\?v=|youtu\.be/)")


def is_youtube_url(s):
    return bool(YOUTUBE_URL_RE.search(s))


def sanitize_name(s):
    """Mirror the SAFE_NAME rule used in transcribe.sh and pipeline.sh."""
    return re.sub(r"[^A-Za-z0-9_-]", "_", s.replace(" ", "_")).strip("_")


def derive_meeting_name(video_path, transcript_path):
    """Pull a sensible meeting name from the input filenames.

    Prefer the transcript filename (which the pipeline always threads with
    a sensible `<safe_name>_<timestamp>` pattern) over the video filename
    (which on the YouTube path is a fixed `video.mp4` and would otherwise
    collapse every YouTube meeting into a single `frames/video/` directory).

    Filenames look like: weekly_standup_20250727_141500.{mp4,txt}
    Strip the extension + trailing timestamp.
    """
    def _strip_timestamp_suffix(stem):
        parts = stem.rsplit("_", 2)
        if len(parts) >= 3 and parts[-1].isdigit() and parts[-2].isdigit() \
                and len(parts[-1]) == 6 and len(parts[-2]) == 8:
            return "_".join(parts[:-2])
        return stem

    if transcript_path:
        src = Path(transcript_path).stem
        name = _strip_timestamp_suffix(src)
        if name:
            return name

    # Fallback: derive from the video filename.
    if video_path:
        return _strip_timestamp_suffix(Path(video_path).stem)

    return "meeting"


def download_youtube_video(url, out_dir):
    """Download a YouTube video as a single MP4 via yt-dlp.

    Returns the path to the downloaded MP4. Caller is responsible for
    cleaning up `out_dir` (and everything under it).

    Note: we deliberately avoid the separate-stream + merge path
    ("bestvideo+bestaudio --merge-output-format mp4"). That merge step now
    requires a JavaScript runtime (deno/node) to be installed for YouTube
    extraction, which is overkill for a single-file download. The single-
    stream "best[ext=mp4]/best" format returns one muxed file yt-dlp can
    hand back without re-muxing, and falls back to webm when mp4 isn't
    available.
    """
    if not shutil.which("yt-dlp"):
        raise SystemExit("yt-dlp is not installed. Run setup.sh first.")

    print(f"==> Downloading YouTube video via yt-dlp")
    out_template = str(Path(out_dir) / "video.%(ext)s")
    cmd = [
        "yt-dlp",
        "--no-playlist",
        # Single-file format. No merge step -> no JS-runtime dependency.
        # On some videos yt-dlp may fall back to webm when no mp4 stream
        # is exposed; the caller accepts whatever container lands in out_dir.
        "-f", "best[ext=mp4]/best",
        "-o", out_template,
        url,
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        sys.stderr.write(proc.stderr)
        raise SystemExit(f"yt-dlp failed with exit code {proc.returncode}")

    # yt-dlp picks the extension from the chosen stream: .mp4 when an mp4
    # stream is available, .webm otherwise. Prefer .mp4; fall back to
    # whatever was produced so we don't fail on webm-only videos.
    candidates = sorted(Path(out_dir).glob("video.*"))
    if not candidates:
        raise SystemExit(f"yt-dlp reported success but no file found in {out_dir}")
    for c in candidates:
        if c.suffix == ".mp4":
            return c
    return candidates[0]


def resolve_prompt_path(prompt_name):
    """Resolve a --prompt/SUMMARY_PROMPT value to a file in prompts/.

    `prompt_name` may be a bare name ("standup"), a name with the .md
    suffix ("standup.md"), or None (falls back to the default
    prompts/summarize.md). Raises SystemExit with a helpful message,
    including the list of available prompts, if the name doesn't
    resolve to an existing file.
    """
    if not prompt_name:
        return PROMPT_PATH

    filename = prompt_name if prompt_name.endswith(".md") else f"{prompt_name}.md"
    path = PROMPTS_DIR / filename

    if not path.is_file():
        available = sorted(p.stem for p in PROMPTS_DIR.glob("*.md"))
        available_str = ", ".join(available) if available else "(none found)"
        raise SystemExit(
            f"Prompt '{prompt_name}' not found at {path}.\n"
            f"Available prompts in {PROMPTS_DIR}: {available_str}"
        )
    return path


def load_prompt_template(prompt_path=PROMPT_PATH):
    """Load the prompt and return the user-prompt skeleton.

    The file contains both the system prompt (everything before # Input)
    and the user-prompt skeleton (the # Input section). We return the
    skeleton since it has the {transcript} and {frame_manifest} placeholders
    that llm_client.summarize fills in."""
    text = Path(prompt_path).read_text()
    if "# Input" in text:
        skeleton = text.split("# Input", 1)[1]
    else:
        skeleton = text
    return skeleton.strip() + "\n\n"


def extract_frames(video_path, meeting_name, frames_dir):
    """Run extract_frames.py as a subprocess. Returns the manifest path."""
    out_dir = Path(frames_dir) / meeting_name
    out_dir.mkdir(parents=True, exist_ok=True)

    cmd = [
        sys.executable,
        str(SCREEN_DIR / "extract_frames.py"),
        str(video_path),
        str(out_dir),
        meeting_name,
    ]
    # Forward the relevant env vars so the subprocess can be tuned the same
    # way as a direct invocation.
    env = os.environ.copy()
    env.setdefault("FRAME_OUTPUT_DIR", DEFAULT_FRAME_DIR)
    print(f"==> Extracting frames -> {out_dir}")
    proc = subprocess.run(cmd, env=env)
    if proc.returncode != 0:
        raise SystemExit(f"extract_frames.py exited with code {proc.returncode}")
    return out_dir / "manifest.json"


def load_manifest(manifest_path):
    """Read manifest.json and return list[FrameMeta]."""
    with open(manifest_path) as f:
        data = json.load(f)
    frames = []
    for entry in data.get("frames", []):
        frames.append(FrameMeta(
            timestamp_s=entry["timestamp_s"],
            kind=entry["kind"],
            path=entry["path"],
        ))
    return frames


def _sweep_stale_yt_tmpdirs():
    """Remove YT download dirs older than YT_STALE_SECONDS.

    A previous run that crashed between download and finally-cleanup will
    leak `meeting-bot-yt-*` dirs here. The finally block does its best,
    but if the process was SIGKILL'd (oom-killer, sudo kill, etc.) the
    cleanup never ran. Sweeping at startup is cheap (one stat per stale
    dir) and keeps /opt/meeting-bot/tmp from filling up over time.
    """
    if not YT_TMP_ROOT.is_dir():
        return
    now = datetime.now().timestamp()
    removed = 0
    for d in YT_TMP_ROOT.glob("meeting-bot-yt-*"):
        try:
            age = now - d.stat().st_mtime
        except OSError:
            continue
        if age < YT_STALE_SECONDS:
            continue
        try:
            shutil.rmtree(d)
            removed += 1
            print(f"==> Swept stale YouTube tempdir: {d} (age {int(age)}s)")
        except OSError as e:
            print(f"==> Could not sweep {d}: {e}", file=sys.stderr)
    if removed:
        print(f"==> Removed {removed} stale YouTube tempdir(s)")


def _extract_prompt_flag(argv):
    """Pull --prompt NAME / --prompt=NAME out of argv.

    Returns (remaining_argv, prompt_name_or_None). Leaves the rest of
    argv's order untouched so the existing positional parsing keeps
    working regardless of where the flag was placed on the command line.
    """
    remaining = []
    prompt_name = None
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg == "--prompt":
            if i + 1 >= len(argv):
                raise SystemExit("--prompt requires a value, e.g. --prompt standup")
            prompt_name = argv[i + 1]
            i += 2
            continue
        if arg.startswith("--prompt="):
            prompt_name = arg.split("=", 1)[1]
            i += 1
            continue
        remaining.append(arg)
        i += 1
    return remaining, prompt_name


def main():
    argv, prompt_name = _extract_prompt_flag(sys.argv)
    if prompt_name is None:
        prompt_name = os.environ.get("SUMMARY_PROMPT")

    if len(argv) < 3:
        print(
            f"Usage: {argv[0]} <video_or_youtube_url> <transcript_path> "
            f"[<output_md_path>] [--prompt NAME]"
        )
        sys.exit(1)

    # Self-heal before doing anything else: stale tempdirs from crashed
    # prior runs can otherwise consume gigabytes under /opt/meeting-bot/tmp.
    _sweep_stale_yt_tmpdirs()

    video_arg = argv[1]
    transcript_path = argv[2]

    # YouTube URLs: download to a temp dir, then run the rest of the flow
    # against the downloaded MP4. Clean up the temp dir at the end.
    yt_tmpdir = None
    if is_youtube_url(video_arg):
        # Always create YT_TMP_ROOT on demand so the script works on a
        # fresh VM where setup.sh hasn't run yet.
        YT_TMP_ROOT.mkdir(parents=True, exist_ok=True)
        yt_tmpdir = tempfile.mkdtemp(prefix="meeting-bot-yt-", dir=str(YT_TMP_ROOT))
        video_arg = str(download_youtube_video(video_arg, yt_tmpdir))
        # If the user passed a name arg, prefer it; otherwise derive from
        # the downloaded filename (yt-dlp names it video.mp4 here, so this
        # is rarely useful - the name comes from the transcript filename).
        if len(argv) <= 4:
            # Override name derivation: use the YouTube video id if we can
            # extract it. yt-dlp puts the title in the file metadata, but
            # not in the filename when we set --output to a fixed template.
            # The transcript filename (positional arg 2) is what the user
            # provided; derive_meeting_name() handles it.
            pass

    meeting_name = derive_meeting_name(video_arg, transcript_path)

    # Output path: explicit arg, or default in /opt/meeting-bot/summaries/.
    if len(argv) > 3:
        output_path = argv[3]
    else:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        out_dir = Path(DEFAULT_SUMMARY_DIR)
        out_dir.mkdir(parents=True, exist_ok=True)
        output_path = str(out_dir / f"{meeting_name}_{stamp}.md")

    try:
        # 1. Extract frames from the video.
        frames_dir = os.environ.get("FRAME_OUTPUT_DIR", DEFAULT_FRAME_DIR)
        manifest_path = extract_frames(video_arg, meeting_name, frames_dir)

        # 2. Load the frame manifest.
        frames = load_manifest(manifest_path)
        if not frames:
            raise SystemExit(f"No frames extracted - check extract_frames.py output above.")
        print(f"==> Loaded {len(frames)} frames from manifest")

        # 3. Read the transcript.
        print(f"==> Reading transcript: {transcript_path}")
        transcript = Path(transcript_path).read_text().strip()

        # 4. Load the prompt skeleton.
        prompt_path = resolve_prompt_path(prompt_name)
        print(f"==> Using prompt: {prompt_path}")
        prompt_template = load_prompt_template(prompt_path)

        # 5. Call the LLM.
        # Two dispatch modes:
        #   - SUMMARY_BACKEND=<one>     -> call that backend, fail on error
        #   - SUMMARY_BACKEND=fallback  -> walk SUMMARY_FALLBACK_CHAIN
        #     (defaults to fcc,nvidia_nim,gemini); the first to succeed wins.
        backend = os.environ.get("SUMMARY_BACKEND", "anthropic").lower()
        if backend == "fallback":
            chain = os.environ.get(
                "SUMMARY_FALLBACK_CHAIN", "fcc,nvidia_nim,gemini"
            )
            print(f"==> Summarizing with fallback chain: {chain}")
            summary = summarize_with_fallback(frames, transcript, prompt_template)
        else:
            print(f"==> Summarizing with backend: {backend}")
            summary = summarize(frames, transcript, prompt_template)

        # 6. Write the output.
        Path(output_path).write_text(summary)
        print(f"==> Wrote summary: {output_path}")

        # 7. Print a short preview to stdout.
        preview_lines = summary.splitlines()[:30]
        print("")
        print("--- preview ---")
        print("\n".join(preview_lines))
        if len(summary.splitlines()) > 30:
            print(f"... ({len(summary.splitlines()) - 30} more lines in {output_path})")
    finally:
        # Clean up the YouTube temp dir if we created one. Don't swallow
        # errors here: a leak here is what created the 1.7GB /tmp incident
        # in the first place. If cleanup fails, the operator needs to see
        # it (the next startup sweep will retry).
        if yt_tmpdir:
            try:
                shutil.rmtree(yt_tmpdir)
            except OSError as e:
                print(
                    f"==> WARNING: failed to remove YouTube tempdir {yt_tmpdir}: {e}",
                    file=sys.stderr,
                )


if __name__ == "__main__":
    main()