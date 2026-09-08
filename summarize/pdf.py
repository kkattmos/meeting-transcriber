#!/usr/bin/env python3
"""
Render a summary document to PDF: the readable deliverable beside the markdown.

Four things happen here that the markdown doesn't need.

  * **Maths gets typeset.** The model writes LaTeX; markdown readers render
    it and WeasyPrint — no JS engine, no MathML — would print the source. So
    every expression is lifted out before the HTML conversion and comes back
    as Computer Modern, set by matplotlib's mathtext. See mathrender.py.

  * **Keyframes go to the back, not into the argument.** A keyframe is a
    screenshot of a video call: mostly a participant's face, a half-drawn
    slide, or (the scene-change pass being drawn to exactly this) solid black.
    Printed full width mid-paragraph they were noise, so by default the
    citations stay as the model wrote them and the frames they name become a
    thumbnail contact sheet in Appendix A — blank ones dropped, each frame
    once, and only the ones actually cited get cropped at all.
    `PDF_FRAMES=inline` restores the old behaviour; `none` drops them.

  * **The transcript is present but invisible.** A PDF has no collapsed
    <details>, and eighty kilobytes of ASR output — as an appendix or at the
    top — buries the summary. It goes in as a white 1pt layer between
    BEGIN_TRANSCRIPT and END_TRANSCRIPT markers instead: the reader never sees
    it, `pdftotext` always finds it. `PDF_TRANSCRIPT=appendix` prints it.

  * **Reference material gets Appendix B**: the slide images collected from
    the GitHub repo or folder passed via --resources, each captioned with the
    file it came from. The provenance comment becomes a real footer line.

WeasyPrint does the rendering: pip-installable, needs no browser, embeds local
images by path, and shapes Thai correctly given a Thai font. The default face
is Adwaita Sans at 8pt with Arial and Liberation Sans behind it and Noto Sans
Thai for the Thai — see DEFAULT_FONT_STACK, and note that dropping the Thai
font from a custom PDF_FONT_FAMILY turns a Thai lecture into tofu boxes.

Nothing here is allowed to take the run down. `render()` raises PdfUnavailable
when the toolchain is missing, and summarize.py turns that into a warning: the
markdown has already been written by then, and a missing PDF is an
inconvenience, not a lost lecture.
"""
import dataclasses
import html
import os
import re
import shutil
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import framecrop  # noqa: E402
import mathrender  # noqa: E402


class PdfUnavailable(RuntimeError):
    """The PDF toolchain isn't installed (weasyprint / markdown)."""


# Adwaita Sans first (installed by setup.sh), Arial next so a box that has the
# real thing uses it, then Liberation Sans — which is what "Arial" resolves to
# on a Debian box without it. Noto Sans Thai has to stay in the stack: Adwaita
# has no Thai glyphs, and a Thai lecture summary in tofu boxes is not a PDF.
DEFAULT_FONT_STACK = ("Adwaita Sans", "Arial", "Liberation Sans",
                      "Noto Sans Thai", "Noto Sans", "DejaVu Sans",
                      "sans-serif")
DEFAULT_FONT_SIZE_PT = 8.0
# Contact-sheet thumbnails are three to a row on an A4 page — about 55mm wide.
# Anything past ~640px of source is detail the print can't show.
DEFAULT_CONTACT_MAX_WIDTH = 640

# *(Frame 12 @ 410.0s)*, (Frame 12), [frame 12 @ 410.0s (scene_change)] — the
# model is told to use the first form, but it is a language model and the other
# two show up often enough to be worth matching.
FRAME_CITE_RE = re.compile(
    r"[\(\[]\s*frames?\s*#?\s*(\d+)[^)\]\n]*[\)\]]", re.IGNORECASE)
SLIDE_CITE_RE = re.compile(
    r"[\(\[]\s*slide\s*#?\s*(\d+)[^)\]\n]*[\)\]]", re.IGNORECASE)
PROVENANCE_RE = re.compile(r"<!--\s*meeting-transcriber(.*?)-->", re.DOTALL)
DETAILS_RE = re.compile(r"<details>(.*?)</details>", re.DOTALL | re.IGNORECASE)
# The same block plus the <br> build_document emits right after it. A combined
# document holds one of these per source, so they are stripped by iteration
# rather than by a count=1 sub followed by removing "the first <br>" — which in
# a combined document was not the one belonging to that block.
DETAILS_BLOCK_RE = re.compile(
    r"<details>(.*?)</details>[ \t]*\n?[ \t]*(?:<br\s*/?>)?",
    re.DOTALL | re.IGNORECASE)


def _font_stack():
    raw = os.environ.get("PDF_FONT_FAMILY")
    if not raw:
        return ", ".join(f'"{f}"' if " " in f else f for f in DEFAULT_FONT_STACK)
    parts = [p.strip() for p in raw.split(",") if p.strip()]
    return ", ".join(f'"{p}"' if " " in p and not p.startswith('"') else p
                     for p in parts)


def _font_size():
    """PDF_FONT_SIZE, in points. Everything else in the sheet is relative."""
    raw = (os.environ.get("PDF_FONT_SIZE") or "").strip()
    if not raw:
        return DEFAULT_FONT_SIZE_PT
    try:
        value = float(raw)
    except ValueError:
        print(f"  warning: PDF_FONT_SIZE={raw!r} is not a number; using "
              f"{DEFAULT_FONT_SIZE_PT}", file=sys.stderr)
        return DEFAULT_FONT_SIZE_PT
    return value if 4.0 <= value <= 24.0 else DEFAULT_FONT_SIZE_PT


def frames_mode():
    """PDF_FRAMES: contact (default), inline, or none.

    `contact` keeps the citations as the model wrote them and collects the
    frames they name into a thumbnail appendix. It is the default because
    inline frames were the export's worst feature: a keyframe is a screenshot
    of a video call, so most of them are a participant's face, a half-drawn
    slide or — the scene-change pass being what it is — solid black, printed
    full width in the middle of an argument they illustrate only by accident.
    """
    value = (os.environ.get("PDF_FRAMES") or "contact").strip().lower()
    if value not in ("contact", "inline", "none"):
        print(f"  warning: PDF_FRAMES={value!r} — expected contact, inline or "
              f"none; using contact", file=sys.stderr)
        return "contact"
    return value


def transcript_mode():
    """PDF_TRANSCRIPT: hidden (default), appendix, or none.

    `hidden` writes the transcript into the page as white 1pt text between
    BEGIN_TRANSCRIPT / END_TRANSCRIPT markers: invisible to a reader, and
    still the first thing `pdftotext` hands an agent. See _hidden_transcript.
    """
    value = (os.environ.get("PDF_TRANSCRIPT") or "hidden").strip().lower()
    if value not in ("hidden", "appendix", "none"):
        print(f"  warning: PDF_TRANSCRIPT={value!r} — expected hidden, "
              f"appendix or none; using hidden", file=sys.stderr)
        return "hidden"
    return value


def _contact_max_width():
    try:
        return int(os.environ.get("PDF_CONTACT_MAX_WIDTH",
                                  str(DEFAULT_CONTACT_MAX_WIDTH)))
    except ValueError:
        return DEFAULT_CONTACT_MAX_WIDTH


def _page_size():
    return (os.environ.get("PDF_PAGE_SIZE") or "A4").strip() or "A4"


def _markdown_to_html(text):
    try:
        import markdown as md
    except ImportError as exc:
        raise PdfUnavailable(
            "the `markdown` package is not installed (pip install markdown)"
        ) from exc
    return md.markdown(
        text,
        extensions=["tables", "fenced_code", "sane_lists", "attr_list", "nl2br"],
        output_format="html5",
    )


def _fmt_timestamp(seconds):
    seconds = int(round(float(seconds)))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


def _split_document(text):
    """Pull the provenance comment and the transcript block out of the body."""
    provenance = {}
    m = PROVENANCE_RE.search(text)
    if m:
        for line in m.group(1).splitlines():
            line = line.strip()
            if not line or ":" not in line:
                continue
            key, value = line.split(":", 1)
            provenance[key.strip()] = value.strip()
        text = PROVENANCE_RE.sub("", text, count=1)

    # Every transcript block, not just the first: a --combine document carries
    # one per source, and leaving the others in the body printed raw <details>
    # markup into the middle of the PDF.
    transcripts = []

    def _take(match):
        inner = re.sub(r"<summary>.*?</summary>", "", match.group(1),
                       flags=re.DOTALL | re.IGNORECASE)
        # The markdown wrapper indents the transcript four spaces so most
        # renderers show it as a code block; undo that here.
        block = "\n".join(line[4:] if line.startswith("    ") else line
                          for line in inner.splitlines()).strip()
        if block:
            transcripts.append(block)
        return ""

    text = DETAILS_BLOCK_RE.sub(_take, text)

    return text.strip(), "\n\n".join(transcripts), provenance


def _prepare_frames(frames, work_dir, crop_mode=None, max_width=None,
                    wanted=None, drop_blank=True):
    """Crop the frames worth printing. Returns {frame_number: info}.

    `wanted` limits the work to the frame numbers the document actually cites
    — in contact-sheet mode that is a handful out of the hundreds a three-hour
    lecture produces, and cropping is the expensive part of this file.
    `drop_blank` discards the solid-black frames the scene-change pass
    collects (see framecrop.is_blank); a dropped frame simply has no picture,
    and its citation stays as text.
    """
    crop_mode = crop_mode or framecrop.crop_mode_from_env()
    max_width = max_width or framecrop.max_width_from_env()
    work_dir = Path(work_dir)
    prepared = {}
    ordered = sorted(frames, key=lambda f: f.timestamp_s)
    for position, frame in enumerate(ordered, start=1):
        # The frame's own global number is what the model was shown and so
        # what the citations name; the position in this list is only the same
        # thing when this list is the whole manifest.
        index = getattr(frame, "number", 0) or position
        if wanted is not None and index not in wanted:
            continue
        src = Path(frame.path)
        if not src.is_file():
            continue
        if drop_blank and framecrop.is_blank(src):
            continue
        dst = work_dir / f"frame_{index:04d}.jpg"
        try:
            out = framecrop.crop_frame(src, dst, mode=crop_mode,
                                       max_width=max_width)
        except Exception as exc:  # noqa: BLE001 - cropping is best-effort
            print(f"  note: frame {index} could not be prepared ({exc})",
                  file=sys.stderr)
            out = src
        prepared[index] = {
            "path": str(Path(out).resolve()),
            "timestamp": frame.timestamp_s,
            "kind": frame.kind,
        }
    return prepared


# Every way the model writes a frame number, including the second and third
# number of a compound citation like "(Frame 33 @ 0:34:40, Frame 15 @ ...)"
# which FRAME_CITE_RE only sees the first of.
FRAME_MENTION_RE = re.compile(r"frames?\s*#?\s*(\d+)", re.IGNORECASE)


def _cited_frame_numbers(html_body):
    """Every frame number the document mentions, in first-mention order."""
    seen = []
    for match in FRAME_MENTION_RE.finditer(html_body):
        number = int(match.group(1))
        if number not in seen:
            seen.append(number)
    return seen


def _figure_html(src, caption):
    return (f'<figure class="frame"><img src="file://{html.escape(src)}" />'
            f'<figcaption>{html.escape(caption)}</figcaption></figure>')


def _inline_citations(html_body, prepared, slide_images):
    """Turn the first citation of each frame/slide into an inline figure.

    Runs on the rendered HTML rather than the markdown so a citation inside a
    table cell or a list item doesn't have a block-level <figure> spliced into
    the middle of it: those are emitted after the closing tag of the paragraph
    they appear in, which is what the placeholder pass below does.
    """
    seen_frames = set()
    seen_slides = set()
    pending = []

    def _frame_sub(match):
        try:
            number = int(match.group(1))
        except ValueError:
            return match.group(0)
        info = prepared.get(number)
        if not info or number in seen_frames:
            return f"(Frame {number})"
        seen_frames.add(number)
        caption = (f"Frame {number} — {_fmt_timestamp(info['timestamp'])}"
                   f" ({info['kind'].replace('_', ' ')})")
        token = f"@@FIGURE{len(pending)}@@"
        pending.append(_figure_html(info["path"], caption))
        return f"(Frame {number}){token}"

    def _slide_sub(match):
        try:
            number = int(match.group(1))
        except ValueError:
            return match.group(0)
        if number < 1 or number > len(slide_images) or number in seen_slides:
            return match.group(0)
        seen_slides.add(number)
        image = slide_images[number - 1]
        token = f"@@FIGURE{len(pending)}@@"
        pending.append(_figure_html(str(Path(image["path"]).resolve()),
                                    f"Slide {number} — {image['label']}"))
        return f"(Slide {number}){token}"

    html_body = FRAME_CITE_RE.sub(_frame_sub, html_body)
    html_body = SLIDE_CITE_RE.sub(_slide_sub, html_body)

    # Hoist each placeholder out to just after the block element it sits in,
    # so figures never land inside a <p>, <td> or <li>.
    for i, figure in enumerate(pending):
        token = f"@@FIGURE{i}@@"
        if token not in html_body:
            continue
        pos = html_body.index(token)
        html_body = html_body.replace(token, "", 1)
        close = _end_of_block(html_body, pos)
        html_body = html_body[:close] + figure + html_body[close:]
    return html_body


_BLOCK_CLOSERS = ("</p>", "</li>", "</tr>", "</table>", "</h1>", "</h2>",
                  "</h3>", "</h4>", "</blockquote>", "</pre>")


def _end_of_block(text, pos):
    """Index just past the end of the block element containing `pos`."""
    best = len(text)
    for closer in _BLOCK_CLOSERS:
        idx = text.find(closer, pos)
        if idx != -1 and idx < best:
            best = idx + len(closer)
    return best


def _css():
    size = _font_size()
    return f"""
@page {{
    size: {_page_size()};
    margin: 18mm 16mm 20mm 16mm;
    @bottom-center {{
        content: counter(page) " / " counter(pages);
        font-size: 7pt;
        color: #777;
    }}
}}
body {{
    font-family: {_font_stack()};
    font-size: {size:g}pt;
    line-height: 1.5;
    color: #16181d;
}}
h1 {{ font-size: 1.9em; margin: 0 0 4pt 0; line-height: 1.25; }}
h2 {{ font-size: 1.35em; margin: 14pt 0 5pt 0; border-bottom: 1px solid #d8dbe0;
      padding-bottom: 3pt; break-after: avoid; }}
h3 {{ font-size: 1.12em; margin: 10pt 0 3pt 0; break-after: avoid; }}
h4 {{ font-size: 1em; margin: 8pt 0 3pt 0; break-after: avoid; }}
p, li {{ orphans: 2; widows: 2; }}
ul, ol {{ margin: 4pt 0 4pt 16pt; padding: 0; }}
code {{ font-family: "DejaVu Sans Mono", monospace; font-size: 0.92em;
        background: #f2f3f5; padding: 0 2px; border-radius: 2px; }}
pre {{ background: #f2f3f5; padding: 6pt; border-radius: 3px;
       font-size: 0.88em; white-space: pre-wrap; word-wrap: break-word; }}
table {{ border-collapse: collapse; width: 100%; margin: 8pt 0;
         font-size: 0.95em; }}
th, td {{ border: 1px solid #d8dbe0; padding: 3pt 5pt; text-align: left;
          vertical-align: top; }}
th {{ background: #f2f3f5; }}
hr {{ border: none; border-top: 1px solid #d8dbe0; margin: 12pt 0; }}
figure.frame {{ margin: 10pt 0; text-align: center; break-inside: avoid; }}
figure.frame img {{ max-width: 100%; max-height: 105mm;
                    border: 1px solid #d8dbe0; border-radius: 3px; }}
figure.frame figcaption {{ font-size: 0.9em; color: #666; margin-top: 3pt; }}
.docmeta {{ font-size: 0.9em; color: #666; margin: 0 0 10pt 0; }}
.docmeta span {{ margin-right: 10pt; }}
.source {{ font-size: 0.95em; color: #333; margin: 0 0 12pt 0;
           word-break: break-all; }}
.appendix {{ break-before: page; }}
.transcript {{ font-size: 0.9em; line-height: 1.45; color: #333;
               white-space: pre-wrap; }}
.notes {{ font-size: 0.9em; color: #8a6d3b; }}

/* Contact sheet: three thumbnails to a row, inline-block rather than grid
   because that lays out identically on every WeasyPrint version we might
   meet on the box. */
.contact {{ margin-top: 6pt; }}
figure.thumb {{ display: inline-block; width: 31.5%; margin: 0 1% 8pt 0;
                text-align: center; vertical-align: top;
                break-inside: avoid; }}
figure.thumb img {{ width: 100%; border: 1px solid #d8dbe0;
                    border-radius: 2px; }}
figure.thumb figcaption {{ font-size: 0.85em; color: #666; margin-top: 2pt; }}

/* Maths. The images carry their own width/height/vertical-align in points,
   computed from what mathtext reported, so there is nothing to size here. */
img.math {{ margin: 0; }}
.math-block {{ display: block; text-align: center; margin: 7pt 0;
               break-inside: avoid; }}
.math-line {{ display: block; margin: 2pt 0; }}
.math-fallback {{ font-family: "CMU Serif", "Latin Modern Roman",
                  "DejaVu Serif", serif; font-style: italic; }}

/* The transcript layer. White on white at 1pt: no reader sees it, every text
   extractor gets it. Not display:none — WeasyPrint would then put nothing in
   the PDF at all, which is the opposite of the point. */
.hidden-transcript {{ color: #ffffff; font-size: 1pt; line-height: 1pt;
                      letter-spacing: 0; word-spacing: 0;
                      overflow-wrap: anywhere; margin: 0; }}
.hidden-next {{ break-before: page; }}
"""


def _meta_html(provenance, extra_meta):
    bits = []
    for key in ("model", "prompt", "run_id", "generated", "source_type"):
        value = provenance.get(key) or extra_meta.get(key)
        if value:
            bits.append(f"<span><b>{html.escape(key)}:</b> "
                        f"{html.escape(str(value))}</span>")
    if not bits:
        return ""
    return f'<p class="docmeta">{"".join(bits)}</p>'


def _appendix_transcript(transcript):
    """The visible transcript appendix — only with PDF_TRANSCRIPT=appendix."""
    if not transcript.strip():
        return ""
    return ('<div class="appendix"><h2>Appendix C — Transcript</h2>'
            f'<div class="transcript">{html.escape(transcript)}</div></div>')


# Poppler (pdftotext, and so anything built on it — including this project's
# own resources.py) stops returning text after roughly 50,000 characters on a
# single page, silently. The PDF itself is complete: pypdf reads all 85k of a
# real lecture transcript back off one page. But an agent reaching for the
# obvious tool would get 60% of it and no warning, so the hidden layer is cut
# into page-sized pieces instead. Measured on this box against WeasyPrint 69 /
# poppler; 40k leaves room for the marker text and for whatever the visible
# content contributes to the same page.
HIDDEN_CHUNK_CHARS = 40000


def _hidden_chunk_chars():
    try:
        value = int(os.environ.get("PDF_HIDDEN_CHUNK_CHARS",
                                   str(HIDDEN_CHUNK_CHARS)))
    except ValueError:
        return HIDDEN_CHUNK_CHARS
    return value if 1000 <= value <= 45000 else HIDDEN_CHUNK_CHARS


def _hidden_transcript(transcript):
    """The transcript as an invisible layer: white, 1pt, marker-delimited.

    The operator reads the summary; agents read the transcript. Printing it
    cost fifteen pages of Thai ASR output nobody looks at, and dropping it
    lost the one copy that travels with the document. So it goes in unseen
    instead: normal flow — not `display: none`, which would put nothing in the
    PDF at all and defeat the whole point — white on white at 1pt.

    The BEGIN_TRANSCRIPT / END_TRANSCRIPT markers are the contract with
    whatever reads this. An agent running `pdftotext` gets the summary
    followed by a labelled transcript rather than a wall of text with no seam
    in it, and can tell where one ends and the other begins.

    Cost: a couple of blank-looking pages at the back on a long lecture, for
    the reason in HIDDEN_CHUNK_CHARS above. That is the price of the layer
    being complete rather than quietly half there.
    """
    if not transcript.strip():
        return ""
    limit = _hidden_chunk_chars()
    text = transcript.strip()
    chunks = [text[i:i + limit] for i in range(0, len(text), limit)] or [""]
    parts = []
    for index, chunk in enumerate(chunks):
        # Only the continuation pieces force a page: the first is allowed to
        # share whatever room is left on the last page of the summary.
        css_class = ("hidden-transcript" if index == 0
                     else "hidden-transcript hidden-next")
        lead = ("BEGIN_TRANSCRIPT (verbatim source transcript, hidden layer) "
                if index == 0 else "")
        tail = " END_TRANSCRIPT" if index == len(chunks) - 1 else ""
        parts.append(f'<div class="{css_class}">{lead}'
                     f'{html.escape(chunk)}{tail}</div>')
    return "".join(parts)


def _appendix_frames(prepared):
    """Appendix A — the cited keyframes, as a thumbnail contact sheet."""
    if not prepared:
        return ""
    parts = ['<div class="appendix"><h2>Appendix A — Keyframes</h2>',
             '<p>Frames referenced in the summary above, in order of '
             'appearance in the recording.</p>',
             '<div class="contact">']
    for number in sorted(prepared):
        info = prepared[number]
        caption = f"Frame {number} — {_fmt_timestamp(info['timestamp'])}"
        parts.append(
            f'<figure class="thumb">'
            f'<img src="file://{html.escape(info["path"])}" />'
            f'<figcaption>{html.escape(caption)}</figcaption></figure>')
    parts.append("</div></div>")
    return "".join(parts)


def _appendix_resources(bundle, slide_images):
    if bundle is None or not getattr(bundle, "files", None):
        return ""
    parts = ['<div class="appendix"><h2>Appendix B — Reference material</h2>']
    if bundle.sources:
        items = "".join(f"<li>{html.escape(s)}</li>" for s in bundle.sources)
        parts.append(f"<ul>{items}</ul>")
    for i, image in enumerate(slide_images, start=1):
        path = Path(image["path"])
        if not path.is_file():
            continue
        parts.append(_figure_html(str(path.resolve()),
                                  f"Slide {i} — {image['label']}"))
    if getattr(bundle, "notes", None):
        notes = "".join(f"<li>{html.escape(n)}</li>" for n in bundle.notes)
        parts.append(f'<ul class="notes">{notes}</ul>')
    parts.append("</div>")
    return "".join(parts)


def collect_slide_images(bundle, limit=60):
    """Flatten a ResourceBundle's images into [{path, label}] for the PDF."""
    images = []
    if bundle is None:
        return images
    for f in getattr(bundle, "files", []):
        for i, image in enumerate(f.images, start=1):
            label = f.label if len(f.images) == 1 else f"{f.label} p.{i}"
            images.append({"path": image, "label": label})
            if len(images) >= limit:
                return images
    return images


def render(markdown_text, output_path, *, frames=(), work_dir=None,
           resources=None, title=None, source=None, crop_mode=None,
           max_width=None):
    """Render `markdown_text` to a PDF at `output_path`. Returns the path.

    Raises PdfUnavailable if weasyprint/markdown aren't installed.
    """
    try:
        from weasyprint import HTML, CSS
    except ImportError as exc:
        raise PdfUnavailable(
            "weasyprint is not installed (pip install weasyprint; on Debian "
            "it also needs libpango — see setup.sh)"
        ) from exc

    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    # A caller that names a work_dir owns it and gets it left behind (the tests
    # inspect the cropped copies that way). One we invent is ours to remove:
    # the cropped frames are pure intermediates — WeasyPrint embeds the image
    # bytes into the PDF, so nothing reads them again after write_pdf returns.
    # Leaving them behind put a .pdf-frames directory of run-independent
    # filenames next to the deliverable, which on a synced PDF_DIR meant every
    # run re-uploading a directory nobody would ever open.
    owned = work_dir is None
    work_dir = Path(work_dir) if work_dir else output_path.parent / ".pdf-frames"
    work_dir.mkdir(parents=True, exist_ok=True)

    # Everything from here on is inside the try: the scratch directory
    # exists by now, and a failure anywhere — a broken document, an
    # unrenderable frame, WeasyPrint itself — must still release it.
    try:
        body_md, transcript, provenance = _split_document(markdown_text)

        # The document's own "# Title" line becomes the PDF title; keep it in the
        # body too so a run without a wrapper still shows a heading.
        doc_title = title or provenance.get("title")
        if not doc_title:
            m = re.search(r"^#\s+(.+)$", body_md, re.MULTILINE)
            doc_title = m.group(1).strip() if m else "Summary"

        slide_images = collect_slide_images(resources)

        # Maths comes out of the markdown *before* the HTML conversion: python-
        # markdown would eat the underscores and backslashes otherwise. It goes
        # back in after the citation passes, so those never have to step over a
        # base64 data: URI. See mathrender.
        body_md, math_exprs = mathrender.extract(body_md)

        body_html = _markdown_to_html(body_md)

        mode = frames_mode()
        if mode == "inline":
            prepared = _prepare_frames(frames, work_dir, crop_mode, max_width)
            body_html = _inline_citations(body_html, prepared, slide_images)
            frame_appendix = ""
        else:
            # Only the frames the document actually cites, and only if they carry
            # a picture. Everything else in a three-hour manifest is unreferenced.
            cited = set(_cited_frame_numbers(body_html))
            prepared = ({} if mode == "none" or not cited else
                        _prepare_frames(frames, work_dir, crop_mode,
                                        _contact_max_width(), wanted=cited))
            frame_appendix = _appendix_frames(prepared)

        body_html = mathrender.restore(
            body_html,
            mathrender.render_all(math_exprs, size_pt=_font_size()))

        source_line = ""
        src = source or provenance.get("source")
        if src:
            source_line = (f'<p class="source"><b>Source:</b> '
                           f'{html.escape(str(src))}</p>')

        t_mode = transcript_mode()
        document = (
            f"<html><head><meta charset='utf-8'>"
            f"<title>{html.escape(doc_title)}</title></head><body>"
            f"{_meta_html(provenance, {'generated': date.today().isoformat()})}"
            f"{source_line}"
            f"{body_html}"
            f"{frame_appendix}"
            f"{_appendix_resources(resources, slide_images)}"
            f"{_appendix_transcript(transcript) if t_mode == 'appendix' else ''}"
            f"{_hidden_transcript(transcript) if t_mode == 'hidden' else ''}"
            f"</body></html>"
        )

        try:
            HTML(string=document, base_url=str(output_path.parent)).write_pdf(
                str(output_path), stylesheets=[CSS(string=_css())])
        finally:
            # In a finally block so a failed render doesn't strand the directory
            # either. Best-effort: a PDF that rendered must not be reported as
            # failed because its scratch directory wouldn't delete.
            if owned:
                shutil.rmtree(work_dir, ignore_errors=True)
    finally:
        # Best-effort: a PDF that rendered must not be reported as
        # failed because its scratch directory wouldn't delete.
        if owned:
            shutil.rmtree(work_dir, ignore_errors=True)
    return output_path


def load_manifest_frames(manifest_path):
    """Read one frames manifest into numbered FrameMeta objects.

    Numbering is global *within that recording*, exactly as the summarize run
    that produced the citations saw it — see llm_client.assign_numbers.
    """
    import json
    from llm_client import FrameMeta, assign_numbers

    data = json.loads(Path(manifest_path).read_text())
    return assign_numbers(
        [FrameMeta(timestamp_s=e["timestamp_s"], kind=e["kind"],
                   path=e["path"]) for e in data.get("frames", [])])


def merge_manifests(manifest_paths):
    """Merge several recordings' manifests for one combined document.

    Returns ``(frames, offsets)``. Frame numbers are only unique inside the
    recording they came from, so document B's "Frame 4" and document A's
    "Frame 4" are different pictures. Rendering them into one PDF without
    renumbering is the silent-mislabelling failure assign_numbers exists to
    prevent, one level up: nothing errors, and half the pictures are wrong.

    So each manifest's numbers are shifted past every manifest before it, and
    the parallel `offsets` list is handed back for
    `document.shift_frame_citations` to apply the same shift to the prose.
    An entry may be None or "" for a source with no frames, which still
    consumes a slot so the offsets stay aligned with the summaries.
    """
    frames, offsets, next_offset = [], [], 0
    for path in manifest_paths:
        offsets.append(next_offset)
        if not path:
            continue
        section = load_manifest_frames(path)
        for frame in section:
            frames.append(dataclasses.replace(
                frame, number=frame.number + next_offset))
        next_offset += len(section)
    return frames, offsets


def want_pdf():
    return (os.environ.get("SUMMARY_WRITE_PDF", "1").strip().lower()
            not in ("0", "false", "no"))


def want_markdown():
    return (os.environ.get("SUMMARY_WRITE_MARKDOWN", "1").strip().lower()
            not in ("0", "false", "no"))


def _main(argv):
    """CLI: `pdf.py <summary.md> <out.pdf> [--frames-manifest P] [--work-dir D]`.

    --work-dir names the directory the cropped frames are written to. Naming
    it also means keeping it: a directory render() invents for itself is
    deleted once the PDF is written, because the crops are intermediates the
    PDF has already absorbed.
    """
    _VALUED = ("--frames-manifest", "--work-dir")
    opts, positional, skip = {}, [], False
    for i, a in enumerate(argv[1:]):
        if skip:
            skip = False
            continue
        if a in _VALUED:
            # Consume the value, or it lands in `positional` and shifts the
            # output path — the previous parser did exactly that and got away
            # with it only because it read just the first two entries.
            opts[a] = argv[i + 2] if i + 2 < len(argv) else None
            skip = True
        elif any(a.startswith(v + "=") for v in _VALUED):
            k, v = a.split("=", 1)
            opts[k] = v
        elif not a.startswith("--"):
            positional.append(a)
    manifest = opts.get("--frames-manifest")
    work_dir = opts.get("--work-dir")
    args = positional
    if len(args) < 2:
        print("Usage: pdf.py <summary.md> <out.pdf> [--frames-manifest PATH] "
              "[--work-dir DIR]", file=sys.stderr)
        return 2

    frames = load_manifest_frames(manifest) if manifest else []
    try:
        out = render(Path(args[0]).read_text(), args[1], frames=frames,
                     work_dir=work_dir)
    except PdfUnavailable as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print(f"==> Wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv))
