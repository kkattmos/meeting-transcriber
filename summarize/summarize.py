#!/usr/bin/env python3
"""
Option 3: Summarize a meeting by sending the transcript + extracted frames
to a vision-capable LLM.

This is the standalone entry point. When pipeline.sh is used, it's called
automatically after Option 1 (record) and Option 2 (transcribe).

Usage:
    python3 summarize/summarize.py <video_or_youtube_url> <transcript_path> [<output_md_path>] [--prompt NAME] [--frames-manifest PATH]

--prompt NAME picks which file in prompts/ to use (e.g. --prompt standup
loads prompts/standup.md). Can also be set via the SUMMARY_PROMPT env var;
the flag takes precedence over the env var. Defaults to prompts/summarize.md
if neither is given. NAME can be given with or without the .md suffix.

--frames-manifest PATH uses an already-extracted manifest.json instead of
running extract_frames.py here. pipeline.sh passes it because it extracts
frames concurrently with transcription; a resumed run also reuses the frames
the previous attempt already paid for.

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
  SUMMARY_BACKEND        "fallback" (default), "gemini", "anthropic",
                         "nvidia_nim", "ollama" — see summarize/llm_client.py
  SUMMARY_FALLBACK_CHAIN default gemini,fcc,nvidia_nim
  GOOGLE_API_KEY / GEMINI_API_KEY   required when backend=gemini
  GEMINI_MODEL           default gemini-2.5-flash
  ANTHROPIC_BASE_URL     default https://api.anthropic.com
  ANTHROPIC_API_KEY      required when backend=anthropic/fcc
  SUMMARY_MODEL          default claude-sonnet-4-5
  OLLAMA_HOST            default http://localhost:11434
  OLLAMA_MODEL           default llava:13b (only if backend=ollama)
  SUMMARY_PROMPT         name of file in prompts/ to use (no .md needed);
                         overridden by --prompt; default: summarize.md

  Transient-failure handling (503 "server is busy", 429, 5xx) — summarize/retry.py:
  SUMMARY_MAX_RETRIES        default 5
  SUMMARY_RETRY_BASE_SECONDS default 2.0
  SUMMARY_RETRY_MAX_SECONDS  default 60.0

  Long transcripts, summarized in parallel then merged — summarize/chunking.py:
  SUMMARY_CHUNK_CHARS    default 24000 (0 disables chunking)
  SUMMARY_CHUNK_OVERLAP  default 800
  SUMMARY_MAX_PARALLEL   default 3
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

import llm_client  # noqa: E402
from llm_client import FrameMeta, summarize  # noqa: E402
import document  # noqa: E402
from chunking import build_chunks  # noqa: E402
from mapreduce import summarize_chunked  # noqa: E402

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
    extraction, which is overkill for a single-file download. Instead the
    format chain prefers a muxed stream and then falls back to a video-only
    one: the video exists solely to extract frames from, so a missing audio
    track costs nothing, and no re-muxing is needed either way. (YouTube
    stopped exposing muxed format 18 on many videos in 2026-09, which is why
    the video-only fallback exists at all.)
    """
    if not shutil.which("yt-dlp"):
        raise SystemExit("yt-dlp is not installed. Run setup.sh first.")

    print(f"==> Downloading YouTube video via yt-dlp")
    out_template = str(Path(out_dir) / "video.%(ext)s")
    cmd = [
        "yt-dlp",
        "--no-playlist",
        # Single-file format. No merge step -> no JS-runtime dependency.
        # Muxed first, then video-only (frames need no audio). On some videos
        # yt-dlp may fall back to webm when no mp4 stream is exposed; the
        # caller accepts whatever container lands in out_dir.
        "-f", "best[ext=mp4]/best/bv*[ext=mp4][vcodec^=avc1][height<=720]/bv*[ext=mp4][height<=720]/bv*[height<=720]/bv*",
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


def _wrap_document(body, *, original_input, source_url, video_path, transcript,
                   title_override, prompt_path, meeting_name):
    """Build the course-note document around the model's summary body."""
    # The source we cite is the URL the user actually gave us. On the pipeline's
    # YouTube path, video_path is a local download, so --source-url carries the
    # original link through.
    source = source_url or original_input
    source_kind = "youtube" if is_youtube_url(str(source)) else "local_file"

    title = title_override
    if not title and source_kind == "youtube":
        title, _upload = document.youtube_metadata(source)
    if not title:
        title = meeting_name

    return document.build_document(
        body,
        source=source,
        source_kind=source_kind,
        title=title,
        transcript=transcript,
        backend=llm_client.LAST_BACKEND,
        model=llm_client.LAST_MODEL,
        prompt_name=Path(prompt_path).name,
        run_id=meeting_name,
    )


FLAGS_WITH_VALUES = ("--prompt", "--frames-manifest", "--source-url",
                     "--title", "--format")


def _extract_flags(argv):
    """Pull the --flag VALUE / --flag=VALUE options out of argv.

    Returns (remaining_argv, options_dict). The positional order of everything
    else is preserved, so a flag can appear anywhere on the command line.
    """
    remaining = []
    options = {}
    i = 0
    while i < len(argv):
        arg = argv[i]
        matched = False
        for flag in FLAGS_WITH_VALUES:
            key = flag.lstrip("-").replace("-", "_")
            if arg == flag:
                if i + 1 >= len(argv):
                    raise SystemExit(f"{flag} requires a value")
                options[key] = argv[i + 1]
                i += 2
                matched = True
                break
            if arg.startswith(flag + "="):
                options[key] = arg.split("=", 1)[1]
                i += 1
                matched = True
                break
        if matched:
            continue
        remaining.append(arg)
        i += 1
    return remaining, options


def main():
    argv, options = _extract_flags(sys.argv)
    prompt_name = options.get("prompt") or os.environ.get("SUMMARY_PROMPT")
    manifest_arg = options.get("frames_manifest")
    source_url = options.get("source_url")
    title_override = options.get("title")
    doc_format = options.get("format") or os.environ.get("SUMMARY_DOC_FORMAT", "auto")

    if len(argv) < 3:
        print(
            f"Usage: {argv[0]} <video_or_youtube_url> <transcript_path> "
            f"[<output_md_path>] [--prompt NAME] [--frames-manifest PATH] "
            f"[--source-url URL] [--title TEXT] [--format auto|always|never]"
        )
        sys.exit(1)

    # Self-heal before doing anything else: stale tempdirs from crashed
    # prior runs can otherwise consume gigabytes under /opt/meeting-bot/tmp.
    _sweep_stale_yt_tmpdirs()

    video_arg = argv[1]
    transcript_path = argv[2]
    # Kept because video_arg is rewritten to a local path on the YouTube branch,
    # and the document header has to cite the link the user actually gave.
    original_input = video_arg

    # YouTube URLs: download to a temp dir, then run the rest of the flow
    # against the downloaded MP4. Clean up the temp dir at the end.
    #
    # Skipped entirely when --frames-manifest is given: the video is only ever
    # needed to extract frames, so once the caller has a manifest there is
    # nothing left to download. pipeline.sh always passes one — this is what
    # stops a YouTube run from downloading the same video twice.
    yt_tmpdir = None
    if is_youtube_url(video_arg) and not manifest_arg:
        # Always create YT_TMP_ROOT on demand so the script works on a
        # fresh VM where setup.sh hasn't run yet.
        YT_TMP_ROOT.mkdir(parents=True, exist_ok=True)
        yt_tmpdir = tempfile.mkdtemp(prefix="meeting-bot-yt-", dir=str(YT_TMP_ROOT))
        video_arg = str(download_youtube_video(video_arg, yt_tmpdir))

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
        # 1. Frames: reuse an already-extracted manifest when the caller has
        #    one. pipeline.sh always does — it extracts frames concurrently
        #    with transcription, and a resumed run reuses what already
        #    succeeded rather than re-running ffmpeg over the whole video.
        if manifest_arg:
            manifest_path = Path(manifest_arg)
            if not manifest_path.is_file():
                raise SystemExit(f"--frames-manifest: no such file: {manifest_path}")
            print(f"==> Using pre-extracted frames: {manifest_path}")
        else:
            frames_dir = os.environ.get("FRAME_OUTPUT_DIR", DEFAULT_FRAME_DIR)
            manifest_path = extract_frames(video_arg, meeting_name, frames_dir)

        # 2. Load the frame manifest.
        frames = load_manifest(manifest_path)
        if not frames:
            raise SystemExit("No frames extracted - check extract_frames.py output above.")
        print(f"==> Loaded {len(frames)} frames from manifest")

        # 3. Read the transcript.
        print(f"==> Reading transcript: {transcript_path}")
        transcript = Path(transcript_path).read_text().strip()

        # 4. Load the prompt skeleton.
        prompt_path = resolve_prompt_path(prompt_name)
        print(f"==> Using prompt: {prompt_path}")
        prompt_template = load_prompt_template(prompt_path)

        # 5. Call the LLM. Dispatch and transient-failure handling live in
        #    llm_client/retry.py; here we only decide single-call vs chunked.
        backend = os.environ.get("SUMMARY_BACKEND",
                                 llm_client.DEFAULT_BACKEND).lower()
        if backend == "fallback":
            chain = os.environ.get("SUMMARY_FALLBACK_CHAIN",
                                   llm_client.DEFAULT_FALLBACK_CHAIN)
            print(f"==> Summarizing with fallback chain: {chain}")
        else:
            print(f"==> Summarizing with backend: {backend}")

        chunks = build_chunks(transcript, frames, transcript_path)
        if chunks:
            summary = summarize_chunked(chunks, prompt_template, summarize)
        else:
            summary = summarize(frames, transcript, prompt_template)

        # 6. Wrap the model's body in the course-note document template, when
        #    the chosen prompt is one of the course-shaped ones. The link,
        #    transcript and provenance are assembled here rather than asked of
        #    the model, so they can't be hallucinated or truncated.
        if document.wants_wrapper(prompt_name, doc_format):
            summary = _wrap_document(
                summary,
                original_input=original_input,
                source_url=source_url,
                video_path=video_arg,
                transcript=transcript,
                title_override=title_override,
                prompt_path=prompt_path,
                meeting_name=meeting_name,
            )

        # 7. Write the output.
        Path(output_path).write_text(summary)
        print(f"==> Wrote summary: {output_path}")

        # 8. Print a short preview to stdout.
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