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
import sys
from datetime import date
from pathlib import Path

# So `import pdf` works from the combine CLI however this file was invoked.
sys.path.insert(0, str(Path(__file__).resolve().parent))

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
                   include_transcript=True, clip=None):
    """Wrap a model-written summary body in the course-note template."""
    generated = generated or date.today().isoformat()

    parts = [provenance_comment(
        source=source,
        source_type=source_kind,
        model=f"{backend}/{model}" if backend and model else (model or backend),
        prompt=prompt_name or "summarize.md",
        run_id=run_id,
        clip=clip,
        generated=generated,
    )]

    if include_chapter_line:
        parts.append(CHAPTER_PLACEHOLDER)

    parts.append(f"# {title or run_id or 'Untitled'}")

    if source_kind == "youtube":
        parts.append(f"Youtube Link: `{source}`")
    elif source_kind == "kaltura":
        # Not "Source File" — a Kaltura lecture is a link like the YouTube one,
        # and the reader needs to be able to click it.
        parts.append(f"Video Link: `{source}`")
    elif source:
        parts.append(f"Source File: `{source}`")

    # Visible, not just in the provenance comment. Every timestamp below this
    # line — the SRT the transcript came from, "Frame 4 @ 0:02:11", the model's
    # own references — is measured from the START OF THE CLIP, because the
    # media was cut before any of them were produced. A reader who doesn't know
    # that will scrub to the wrong place in the source video and conclude the
    # summary is wrong.
    if clip:
        parts.append(
            f"Clip: `{clip}` of the source. "
            "Timestamps below are relative to the start of the clip."
        )

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


# Every way the model writes a frame number. Deliberately the same shape as
# pdf.FRAME_MENTION_RE, which is what resolves a citation to a picture: if the
# two ever disagree, a citation gets shifted here and looked up there under the
# old number, or the reverse. test_shift_agrees_with_the_pdf_matcher holds them
# together.
FRAME_MENTION_RE = re.compile(r"frames?\s*#?\s*(\d+)", re.IGNORECASE)
# A transcript block, so citation shifting can step over it.
DETAILS_BLOCK_RE = re.compile(r"<details>.*?</details>",
                              re.DOTALL | re.IGNORECASE)


def shift_frame_citations(text, offset):
    """Add `offset` to every "Frame N" citation in a document's prose.

    Frame numbers are unique only within one recording, so combining several
    documents into one PDF has to renumber them — see pdf.merge_manifests for
    why, and for the other half of this operation.

    The transcript block is stepped over on purpose. It is verbatim speech: a
    lecturer saying "frame 3" is not a citation, and rewriting it would both
    corrupt the transcript the PDF carries and invent a citation pointing at a
    picture nobody referenced.
    """
    if not offset:
        return text

    def _bump(match):
        return f"{match.group(0)[:match.start(1) - match.start(0)]}" \
               f"{int(match.group(1)) + offset}"

    out, last = [], 0
    for block in DETAILS_BLOCK_RE.finditer(text):
        out.append(FRAME_MENTION_RE.sub(_bump, text[last:block.start()]))
        out.append(block.group(0))
        last = block.end()
    out.append(FRAME_MENTION_RE.sub(_bump, text[last:]))
    return "".join(out)


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
        document.py combine --output chapter3.md --pdf-out chapter3.pdf \
                    --frames-manifest a.json --frames-manifest b.json a.md b.md

    --frames-manifest is repeatable and positional: the Nth one belongs to the
    Nth summary, and "-" (or "") stands in for a summary with no frames so the
    two lists stay aligned. Giving them is what makes the combined PDF's frame
    citations resolve to the right pictures; see pdf.merge_manifests.
    """
    import argparse

    ap = argparse.ArgumentParser(description="Combine per-run summaries")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("combine")
    p.add_argument("--output", required=True)
    p.add_argument("--pdf-out")
    p.add_argument("--frames-manifest", action="append", default=[],
                   metavar="PATH")
    p.add_argument("--no-chapter-line", action="store_true")
    p.add_argument("summaries", nargs="+")
    args = ap.parse_args()

    manifests = [None if m in ("-", "") else m for m in args.frames_manifest]
    if manifests and len(manifests) != len(args.summaries):
        # Misaligned lists would renumber the wrong sections by the wrong
        # amounts, and the only symptom is a PDF full of confidently wrong
        # pictures. Refuse rather than guess.
        print(f"ERROR: {len(manifests)} --frames-manifest for "
              f"{len(args.summaries)} summaries — pass one per summary "
              f"(use '-' for a summary with no frames)", file=sys.stderr)
        return 2

    frames, offsets = [], None
    if manifests:
        import pdf as pdf_mod
        try:
            frames, offsets = pdf_mod.merge_manifests(manifests)
        except (OSError, ValueError, KeyError) as exc:
            # A manifest that won't parse costs the pictures, not the chapter
            # file. Same rule as a failed render.
            print(f"WARNING: could not merge frame manifests ({exc}); the "
                  f"combined PDF will have no frames", file=sys.stderr)
            frames, offsets = [], None

    text = combine_documents(args.summaries,
                             chapter_line=not args.no_chapter_line,
                             frame_offsets=offsets)
    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text)
    print(f"==> Combined {len(args.summaries)} summary/summaries -> {out}")

    if args.pdf_out:
        # The markdown is the artifact; a PDF that will not render is a
        # warning, exactly as it is for a single run.
        import pdf as pdf_mod
        try:
            written = pdf_mod.render(text, args.pdf_out, frames=frames,
                                     title=out.stem)
            print(f"==> Combined PDF -> {written}")
        except pdf_mod.PdfUnavailable as exc:
            print(f"WARNING: combined PDF {args.pdf_out} not written ({exc})",
                  file=sys.stderr)
        except Exception as exc:  # noqa: BLE001 - never fail on the PDF
            print(f"WARNING: combined PDF {args.pdf_out} failed to render "
                  f"({exc})", file=sys.stderr)
    return 0


def combine_documents(paths, chapter_line=True, frame_offsets=None):
    """Concatenate per-run documents into one chapter-file-shaped markdown.

    Each document already ends with the <br><br> separator, so sections just
    follow one another the way chapter2.md lays them out.

    `frame_offsets`, when given, is one integer per path (the value
    pdf.merge_manifests hands back) and shifts that section's frame citations
    into the combined document's global numbering. Left out, nothing is
    renumbered and the markdown is byte-for-byte what it always was — which is
    the right answer when no PDF is being rendered, since a reader of the .md
    resolves "Frame 4" against that section's own recording.
    """
    chunks = []
    if chapter_line:
        chunks.append(CHAPTER_PLACEHOLDER + "\n")
    for index, path in enumerate(paths):
        try:
            text = Path(path).read_text()
        except OSError:
            continue
        text = strip_provenance_and_chapter(text)
        if frame_offsets:
            # Not `frame_offsets[index]` unguarded: a caller that passed a
            # short list would silently renumber some sections and not others.
            try:
                offset = frame_offsets[index]
            except IndexError:
                raise ValueError(
                    f"frame_offsets has {len(frame_offsets)} entries for "
                    f"{len(paths)} summaries") from None
            text = shift_frame_citations(text, offset)
        chunks.append(text.rstrip() + "\n")
    return "\n".join(chunks)


if __name__ == "__main__":
    raise SystemExit(_main())
