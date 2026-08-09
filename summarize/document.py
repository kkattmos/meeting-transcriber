#!/usr/bin/env python3
"""
Assemble the final markdown document around the model's summary.

The wrapper is built here, in code, rather than asked for in the prompt. Two
reasons: the source link and provenance can't be hallucinated or truncated if
the model never writes them, and the transcript (~80KB on a typical lecture)
never has to make a round trip through the model just to be echoed back.

The shape matches the course-note template — see
`2_Transcripts/_template_lecture_summary.md` and the chapter files next to it:

    <!-- meeting-transcriber provenance ... -->
    Chapter N — <topic> (<date>)

    # <video title>

    Youtube Link: `https://www.youtube.com/watch?v=...`

    <details>
        <summary> View Transcript </summary>

        <transcript, indented four spaces>
    </details>
    <br>

    ...the model's structured summary...

    <br><br>

The `Chapter N — <topic> (<date>)` line is emitted as a literal placeholder, on
purpose: the chapter number isn't derivable from the video and a guess that
looks right but isn't would be worse than an obvious blank to fill in.

The four-space indent inside <details> is deliberate too. It makes most
renderers show the transcript as a code block, which is what the existing
chapter files already do — reproducing them beats "fixing" them.
"""
import re
import shutil
import subprocess
from datetime import date
from pathlib import Path

CHAPTER_PLACEHOLDER = "Chapter N — <topic> (<date>)"
SECTION_SEPARATOR = "<br><br>"

# Prompts whose output is course-note shaped. meeting-* keeps the plain
# executive summary — those don't go into the course files.
WRAPPED_PROMPT_PREFIXES = ("lecture", "tutorial")


def wants_wrapper(prompt_name, mode="auto"):
    """Should this run's summary be wrapped in the course-note template?

    mode: "auto" (decide from the prompt name), "always", or "never".
    """
    if mode == "always":
        return True
    if mode == "never":
        return False
    if not prompt_name:
        return False
    stem = Path(prompt_name).stem.lower()
    return stem.startswith(WRAPPED_PROMPT_PREFIXES)


def youtube_metadata(url, timeout=60):
    """(title, upload_date) for a YouTube URL via yt-dlp, or (None, None).

    Best-effort by design: a missing title should downgrade the heading, never
    fail a summarize stage that has already done all the expensive work.
    """
    if not shutil.which("yt-dlp"):
        return None, None
    try:
        proc = subprocess.run(
            ["yt-dlp", "--no-playlist", "--skip-download",
             "--print", "%(title)s", "--print", "%(upload_date)s", url],
            capture_output=True, text=True, timeout=timeout,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None, None
    if proc.returncode != 0:
        return None, None
    lines = [ln.strip() for ln in proc.stdout.splitlines() if ln.strip()]
    title = lines[0] if lines else None
    upload = lines[1] if len(lines) > 1 else None
    if upload and not re.fullmatch(r"\d{8}", upload):
        upload = None
    return title, upload


def _indent_transcript(text, spaces=4):
    """Indent every line by `spaces`, matching the existing chapter files."""
    pad = " " * spaces
    return "\n".join(pad + line if line.strip() else "" for line in text.splitlines())


def provenance_comment(**fields):
    """The HTML comment header: invisible when rendered, greppable in the raw file.

    It survives being pasted into a bigger chapter document without adding any
    visual noise, which is the whole point of putting it in a comment.
    """
    lines = ["<!-- meeting-transcriber"]
    for key, value in fields.items():
        if value:
            # A "-->" inside a value would end the comment early and dump the
            # rest of the header into the rendered page.
            safe = str(value).replace("-->", "--&gt;")
            lines.append(f"     {key}: {safe}")
    lines.append("-->")
    return "\n".join(lines)


def build_document(body, *, source, source_kind, title=None, transcript="",
                   backend=None, model=None, prompt_name=None, run_id=None,
                   generated=None, include_chapter_line=True,
                   include_transcript=True):
    """Wrap a model-written summary body in the course-note template."""
    generated = generated or date.today().isoformat()

    parts = [provenance_comment(
        source=source,
        source_type=source_kind,
        model=f"{backend}/{model}" if backend and model else (model or backend),
        prompt=prompt_name or "summarize.md",
        run_id=run_id,
        generated=generated,
    )]

    if include_chapter_line:
        parts.append(CHAPTER_PLACEHOLDER)

    parts.append(f"# {title or run_id or 'Untitled'}")

    if source_kind == "youtube":
        parts.append(f"Youtube Link: `{source}`")
    elif source:
        parts.append(f"Source File: `{source}`")

    if include_transcript:
        transcript_block = _indent_transcript(transcript.strip()) if transcript.strip() \
            else "    *(transcript unavailable)*"
        parts.append(
            "<details>\n"
            "    <summary> View Transcript </summary>\n\n"
            f"{transcript_block}\n"
            "</details>\n"
            "<br>"
        )

    parts.append(body.strip())
    parts.append(SECTION_SEPARATOR)

    return "\n\n".join(parts) + "\n"


def strip_provenance_and_chapter(text):
    """Remove the header comment and the Chapter placeholder from a document.

    Used when concatenating several documents into one chapter file, where the
    Chapter line belongs once at the top rather than above every video.
    """
    text = re.sub(r"<!--\s*meeting-transcriber.*?-->\s*", "", text,
                  count=1, flags=re.DOTALL)
    text = text.replace(CHAPTER_PLACEHOLDER + "\n", "", 1)
    return text.lstrip("\n")


def _main():
    """CLI used by pipeline.sh --combine.

        document.py combine --output chapter3.md a.md b.md c.md
    """
    import argparse

    ap = argparse.ArgumentParser(description="Combine per-run summaries")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("combine")
    p.add_argument("--output", required=True)
    p.add_argument("--no-chapter-line", action="store_true")
    p.add_argument("summaries", nargs="+")
    args = ap.parse_args()

    text = combine_documents(args.summaries,
                             chapter_line=not args.no_chapter_line)
    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text)
    print(f"==> Combined {len(args.summaries)} summary/summaries -> {out}")
    return 0


def combine_documents(paths, chapter_line=True):
    """Concatenate per-run documents into one chapter-file-shaped markdown.

    Each document already ends with the <br><br> separator, so sections just
    follow one another the way chapter2.md lays them out.
    """
    chunks = []
    if chapter_line:
        chunks.append(CHAPTER_PLACEHOLDER + "\n")
    for path in paths:
        try:
            text = Path(path).read_text()
        except OSError:
            continue
        chunks.append(strip_provenance_and_chapter(text).rstrip() + "\n")
    return "\n".join(chunks)


if __name__ == "__main__":
    raise SystemExit(_main())
