#!/usr/bin/env python3
"""
Render a summary document to PDF, with the video frames it cites shown inline.

The markdown summary is the primary artifact; this is the readable one. The
model cites keyframes as *(Frame 12 @ 410.0s)* — in markdown that's a
dangling reference to a JPEG nobody opens, so here the first citation of each
frame becomes the actual picture, cropped down to the slide (see
framecrop.py), captioned with its timestamp. Later citations of the same frame
stay as plain text so a lecture that keeps referring back to one diagram
doesn't print it eight times.

Layout decisions:

  * The transcript moves to the back. In markdown it lives in a collapsed
    <details> block; a PDF has no "collapsed", and 80KB of ASR output at the
    top of the document would bury the summary. It becomes "Appendix A —
    Transcript" on its own page, in a smaller face.
  * Reference material gets Appendix B: the slide/page images collected from
    the GitHub repo or folder passed via --resources, each captioned with the
    file it came from.
  * The provenance comment becomes a real footer line (model, prompt, run id,
    date) instead of an invisible HTML comment.

WeasyPrint does the rendering: pip-installable, needs no browser, embeds local
images by path, and shapes Thai correctly given a Thai font (the page CSS asks
for Noto Sans Thai first, and setup.sh installs it).

Nothing here is allowed to take the run down. `render()` raises PdfUnavailable
when the toolchain is missing, and summarize.py turns that into a warning: the
markdown has already been written by then, and a missing PDF is an
inconvenience, not a lost lecture.
"""
import html
import os
import re
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import framecrop  # noqa: E402


class PdfUnavailable(RuntimeError):
    """The PDF toolchain isn't installed (weasyprint / markdown)."""


DEFAULT_FONT_STACK = ("Noto Sans Thai", "Noto Sans", "DejaVu Sans", "sans-serif")

# *(Frame 12 @ 410.0s)*, (Frame 12), [frame 12 @ 410.0s (scene_change)] — the
# model is told to use the first form, but it is a language model and the other
# two show up often enough to be worth matching.
FRAME_CITE_RE = re.compile(
    r"[\(\[]\s*frames?\s*#?\s*(\d+)[^)\]\n]*[\)\]]", re.IGNORECASE)
SLIDE_CITE_RE = re.compile(
    r"[\(\[]\s*slide\s*#?\s*(\d+)[^)\]\n]*[\)\]]", re.IGNORECASE)
PROVENANCE_RE = re.compile(r"<!--\s*meeting-transcriber(.*?)-->", re.DOTALL)
DETAILS_RE = re.compile(r"<details>(.*?)</details>", re.DOTALL | re.IGNORECASE)


def _font_stack():
    raw = os.environ.get("PDF_FONT_FAMILY")
    if not raw:
        return ", ".join(f'"{f}"' if " " in f else f for f in DEFAULT_FONT_STACK)
    parts = [p.strip() for p in raw.split(",") if p.strip()]
    return ", ".join(f'"{p}"' if " " in p and not p.startswith('"') else p
                     for p in parts)


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

    transcript = ""
    d = DETAILS_RE.search(text)
    if d:
        inner = d.group(1)
        inner = re.sub(r"<summary>.*?</summary>", "", inner,
                       flags=re.DOTALL | re.IGNORECASE)
        # The markdown wrapper indents the transcript four spaces so most
        # renderers show it as a code block; undo that here.
        transcript = "\n".join(line[4:] if line.startswith("    ") else line
                               for line in inner.splitlines()).strip()
        text = DETAILS_RE.sub("", text, count=1)
        text = text.replace("<br>", "", 1)

    return text.strip(), transcript, provenance


def _prepare_frames(frames, work_dir, crop_mode=None, max_width=None):
    """Crop every frame once, up front. Returns {frame_number: info}."""
    crop_mode = crop_mode or framecrop.crop_mode_from_env()
    max_width = max_width or framecrop.max_width_from_env()
    work_dir = Path(work_dir)
    prepared = {}
    ordered = sorted(frames, key=lambda f: f.timestamp_s)
    for index, frame in enumerate(ordered, start=1):
        src = Path(frame.path)
        if not src.is_file():
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
    return f"""
@page {{
    size: {_page_size()};
    margin: 18mm 16mm 20mm 16mm;
    @bottom-center {{
        content: counter(page) " / " counter(pages);
        font-size: 8pt;
        color: #777;
    }}
}}
body {{
    font-family: {_font_stack()};
    font-size: 10.5pt;
    line-height: 1.5;
    color: #16181d;
}}
h1 {{ font-size: 20pt; margin: 0 0 4pt 0; line-height: 1.25; }}
h2 {{ font-size: 14pt; margin: 16pt 0 6pt 0; border-bottom: 1px solid #d8dbe0;
      padding-bottom: 3pt; break-after: avoid; }}
h3 {{ font-size: 11.5pt; margin: 12pt 0 4pt 0; break-after: avoid; }}
p, li {{ orphans: 2; widows: 2; }}
ul, ol {{ margin: 4pt 0 4pt 18pt; padding: 0; }}
code {{ font-family: "DejaVu Sans Mono", monospace; font-size: 9pt;
        background: #f2f3f5; padding: 0 2px; border-radius: 2px; }}
pre {{ background: #f2f3f5; padding: 6pt; border-radius: 3px;
       font-size: 8.5pt; white-space: pre-wrap; word-wrap: break-word; }}
table {{ border-collapse: collapse; width: 100%; margin: 8pt 0;
         font-size: 9pt; }}
th, td {{ border: 1px solid #d8dbe0; padding: 4pt 6pt; text-align: left;
          vertical-align: top; }}
th {{ background: #f2f3f5; }}
hr {{ border: none; border-top: 1px solid #d8dbe0; margin: 14pt 0; }}
figure.frame {{ margin: 10pt 0; text-align: center; break-inside: avoid; }}
figure.frame img {{ max-width: 100%; max-height: 105mm;
                    border: 1px solid #d8dbe0; border-radius: 3px; }}
figure.frame figcaption {{ font-size: 8.5pt; color: #666; margin-top: 3pt; }}
.docmeta {{ font-size: 8.5pt; color: #666; margin: 0 0 10pt 0; }}
.docmeta span {{ margin-right: 10pt; }}
.source {{ font-size: 9.5pt; color: #333; margin: 0 0 12pt 0;
           word-break: break-all; }}
.appendix {{ break-before: page; }}
.transcript {{ font-size: 8.5pt; line-height: 1.45; color: #333;
               white-space: pre-wrap; }}
.notes {{ font-size: 8.5pt; color: #8a6d3b; }}
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
    if not transcript.strip():
        return ""
    return ('<div class="appendix"><h2>Appendix A — Transcript</h2>'
            f'<div class="transcript">{html.escape(transcript)}</div></div>')


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
    work_dir = Path(work_dir) if work_dir else output_path.parent / ".pdf-frames"
    work_dir.mkdir(parents=True, exist_ok=True)

    body_md, transcript, provenance = _split_document(markdown_text)

    # The document's own "# Title" line becomes the PDF title; keep it in the
    # body too so a run without a wrapper still shows a heading.
    doc_title = title or provenance.get("title")
    if not doc_title:
        m = re.search(r"^#\s+(.+)$", body_md, re.MULTILINE)
        doc_title = m.group(1).strip() if m else "Summary"

    prepared = _prepare_frames(frames, work_dir, crop_mode, max_width)
    slide_images = collect_slide_images(resources)

    body_html = _markdown_to_html(body_md)
    body_html = _inline_citations(body_html, prepared, slide_images)

    source_line = ""
    src = source or provenance.get("source")
    if src:
        source_line = (f'<p class="source"><b>Source:</b> '
                       f'{html.escape(str(src))}</p>')

    document = (
        f"<html><head><meta charset='utf-8'>"
        f"<title>{html.escape(doc_title)}</title></head><body>"
        f"{_meta_html(provenance, {'generated': date.today().isoformat()})}"
        f"{source_line}"
        f"{body_html}"
        f"{_appendix_resources(resources, slide_images)}"
        f"{_appendix_transcript(transcript)}"
        f"</body></html>"
    )

    HTML(string=document, base_url=str(output_path.parent)).write_pdf(
        str(output_path), stylesheets=[CSS(string=_css())])
    return output_path


def want_pdf():
    return (os.environ.get("SUMMARY_WRITE_PDF", "1").strip().lower()
            not in ("0", "false", "no"))


def want_markdown():
    return (os.environ.get("SUMMARY_WRITE_MARKDOWN", "1").strip().lower()
            not in ("0", "false", "no"))


def _main(argv):
    """CLI: `pdf.py <summary.md> <out.pdf> [--frames-manifest PATH]`."""
    import json
    args = [a for a in argv[1:] if not a.startswith("--")]
    manifest = None
    for i, a in enumerate(argv):
        if a == "--frames-manifest" and i + 1 < len(argv):
            manifest = argv[i + 1]
        elif a.startswith("--frames-manifest="):
            manifest = a.split("=", 1)[1]
    if len(args) < 2:
        print("Usage: pdf.py <summary.md> <out.pdf> [--frames-manifest PATH]",
              file=sys.stderr)
        return 2

    frames = []
    if manifest:
        from llm_client import FrameMeta
        data = json.loads(Path(manifest).read_text())
        frames = [FrameMeta(timestamp_s=e["timestamp_s"], kind=e["kind"],
                            path=e["path"]) for e in data.get("frames", [])]
    try:
        out = render(Path(args[0]).read_text(), args[1], frames=frames)
    except PdfUnavailable as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print(f"==> Wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv))
