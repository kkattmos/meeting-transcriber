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


def _source_line(source, source_kind, tag=""):
    """The clickable (or at least copyable) line naming where a video came from."""
    if source_kind == "youtube":
        return f"Youtube Link{tag}: `{source}`"
    if source_kind == "kaltura":
        # Not "Source File" — a Kaltura lecture is a link like the YouTube one,
        # and the reader needs to be able to click it.
        return f"Video Link{tag}: `{source}`"
    if source:
        return f"Source File{tag}: `{source}`"
    return None


def _clip_line(clip, tag=""):
    # Visible, not just in the provenance comment. Every timestamp below this
    # line — the SRT the transcript came from, "Frame 4 @ 0:02:11", the model's
    # own references — is measured from the START OF THE CLIP, because the
    # media was cut before any of them were produced. A reader who doesn't know
    # that will scrub to the wrong place in the source video and conclude the
    # summary is wrong.
    if tag:
        # A combined document says once, below the links, that every clock is
        # its own video's; repeating it per line would just bury that.
        return f"Clip{tag}: `{clip}` of that video."
    return (f"Clip: `{clip}` of the source. "
            "Timestamps below are relative to the start of the clip.")


def build_document(body, *, source, source_kind, title=None, transcript="",
                   backend=None, model=None, prompt_name=None, run_id=None,
                   generated=None, include_chapter_line=True,
                   include_transcript=True, clip=None, videos=None):
    """Wrap a model-written summary body in the course-note template.

    `videos`, when given, is the list of sources a --combine summary was made
    from — dicts with `source`, `kind`, and optionally `title` and `clip`, in
    the order the model saw them — and the wrapper then lists one link line
    per video, tagged "(Video N)" so the model's "video 2" references and the
    "Frame 12 @ video 2 ..." citations can be followed back to a link. The
    document keeps a single title (the first video's, or `title`), because it
    is one document about one topic, not a stack of sections.
    """
    generated = generated or date.today().isoformat()

    provenance = dict(
        source=source,
        source_type=source_kind,
        model=f"{backend}/{model}" if backend and model else (model or backend),
        prompt=prompt_name or "summarize.md",
        run_id=run_id,
        clip=clip,
        generated=generated,
    )
    if videos:
        # One line per video in the comment too, so the raw file says which
        # sources went into it without anyone parsing the link lines.
        provenance["source_type"] = "combined"
        provenance["videos"] = len(videos)
        for n, video in enumerate(videos, start=1):
            provenance[f"video_{n}"] = video.get("source")
            if video.get("clip"):
                provenance[f"video_{n}_clip"] = video["clip"]
    parts = [provenance_comment(**provenance)]

    if include_chapter_line:
        parts.append(CHAPTER_PLACEHOLDER)

    if videos and not title:
        title = next((v.get("title") for v in videos if v.get("title")), None)
    parts.append(f"# {title or run_id or 'Untitled'}")

    if videos:
        lines = []
        for n, video in enumerate(videos, start=1):
            line = _source_line(video.get("source"), video.get("kind"),
                                tag=f" (Video {n})")
            if line:
                lines.append(line)
            if video.get("clip"):
                lines.append(_clip_line(video["clip"], tag=f" (Video {n})"))
        # The one fact a reader of a combined summary has to be told: the
        # clock restarts at every video. "0:12:30" alone does not say which.
        lines.append(f"Summarized from {len(videos)} videos as one. "
                     "Every timestamp is relative to the start of the video "
                     "(or clip) it cites.")
        parts.append("\n".join(lines))
    else:
        line = _source_line(source, source_kind)
        if line:
            parts.append(line)
        if clip:
            parts.append(_clip_line(clip))

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
