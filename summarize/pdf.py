#!/usr/bin/env python3
"""
Render a summary document to PDF: the readable deliverable beside the markdown.

Four things happen here that the markdown doesn't need.

  * **Maths gets typeset.** The model writes LaTeX; markdown readers render
    it and WeasyPrint — no JS engine, no MathML — would print the source. So
    every expression is lifted out before the HTML conversion and comes back
    as Computer Modern, set by matplotlib's mathtext. See mathrender.py.

  * **Nested bullets are re-indented.** The model writes sub-items two
    spaces in, like every markdown reader accepts; python-markdown wants four
    and flattens anything less into the parent list. See
    _normalize_list_indent.

  * **Frame citations are faded.** `(Video 1, Frame 52 @ 0:08:52)` after
    every second sentence is what lets a reader scrub to the moment, and also
    what makes the notes hard to read. They stay, at 30% opacity.

  * **The sheet is the summary alone, by default.** The keyframe contact
    sheet (`PDF_FRAMES=contact`), the reference slides (`PDF_RESOURCES=
    appendix`) and the transcript (`PDF_TRANSCRIPT=hidden|appendix`) are all
    off unless asked for. A keyframe is a screenshot of a video call — mostly
    a face, a half-drawn slide or solid black — and a study sheet with forty
    of them at the back, plus a few blank-looking pages of white 1pt
    transcript, is not the document the operator prints. The markdown still
    carries the transcript in its <details> block.

The look — title block, navy section banners, colour-coded callout boxes,
tables, the dark editor panel for code — is specified in DESIGN.md at the
repository root; _css() is its implementation, and the two change together.
The prompts write the callouts as `> [!CONCEPT] Title` blockquotes, which
_extract_callouts turns into boxes; see there.

WeasyPrint does the rendering: pip-installable, needs no browser, embeds local
images by path, and shapes Thai correctly given a Thai font. The body face is
the run's choice (fontchoice.py: Bai Jamjuree or Sarabun for Thai, CMU Serif,
Sarabun or Bai Jamjuree for English), recorded in the provenance as `font`,
and is scaled so every choice looks the size PDF_FONT_SIZE names. Maths —
typeset formulas and any stray ω or ≤ in the prose — is always Computer
Modern; code is always JetBrains Mono. A custom PDF_FONT_FAMILY replaces the
body stack wholesale, and dropping the Thai face from it turns a Thai
lecture into tofu boxes.

Nothing here is allowed to take the run down. `render()` raises PdfUnavailable
when the toolchain is missing, and summarize.py turns that into a warning: the
markdown has already been written by then, and a missing PDF is an
inconvenience, not a lost lecture.
"""
import html
import os
import re
import shutil
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import fontchoice  # noqa: E402
import framecrop  # noqa: E402
import mathrender  # noqa: E402


class PdfUnavailable(RuntimeError):
    """The PDF toolchain isn't installed (weasyprint / markdown)."""


# The body face is chosen by the language the summary is written in
# (SUMMARY_LANGUAGE, recorded in the document's provenance as `language`),
# unless PDF_FONT_FAMILY names a stack, which then applies to both.
#
# English: CMU Serif is Computer Modern (Debian: fonts-cmu, installed by
# setup.sh), the face the maths is already set in, so text and formulae
# match. Latin Modern is the same design under another name for a box that
# has that instead. Noto Serif Thai has to stay in the stack: Computer
# Modern has no Thai glyphs, and a Thai proper noun in tofu boxes is not a
# PDF.
DEFAULT_FONT_STACK_EN = ("CMU Serif", "Latin Modern Roman", "Noto Serif Thai",
                         "Noto Sans Thai", "Noto Serif", "Liberation Serif",
                         "DejaVu Serif", "serif")
# Thai: Bai Jamjuree, then Sarabun — the operator's choice, in that order.
# Both are OFL Google Fonts vendored under fonts/ and installed by setup.sh;
# neither is in Debian's archive. They lead the stack so Latin words inside
# a Thai sentence are set in the same face rather than flipping to a serif.
# The maths is untouched by any of this: mathtext sets it in Computer
# Modern and ships it as SVG (see mathrender).
DEFAULT_FONT_STACK_TH = ("Bai Jamjuree", "Sarabun", "Noto Serif Thai",
                         "Noto Sans Thai", "CMU Serif", "Noto Serif",
                         "Liberation Serif", "DejaVu Serif", "serif")
# The historical name; the English stack, which every render used before
# SUMMARY_LANGUAGE existed.
DEFAULT_FONT_STACK = DEFAULT_FONT_STACK_EN
DEFAULT_FONT_STACKS = {"en": DEFAULT_FONT_STACK_EN, "th": DEFAULT_FONT_STACK_TH}
# Nominal size, as Computer Modern: the other faces are scaled to the same
# x-height (fontchoice.size_factor). 9.5pt since 2026-09-29, after the
# operator's exercise sheet — 8pt read as small print.
DEFAULT_FONT_SIZE_PT = 9.5
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
# The same block plus the <br> build_document emits right after it. Stripped by
# iteration rather than by a count=1 sub followed by removing "the first <br>":
# a document pasted together from several summaries holds one of these per
# source, and the old way left the later blocks' markup in the body.
DETAILS_BLOCK_RE = re.compile(
    r"<details>(.*?)</details>[ \t]*\n?[ \t]*(?:<br\s*/?>)?",
    re.DOTALL | re.IGNORECASE)


def _document_language(provenance=None):
    """The language the document was written in: its provenance field when
    it has one (so a Thai sheet re-rendered on a box now set to English keeps
    its Thai face), else SUMMARY_LANGUAGE, else the default. Never raises —
    a typo in the variable is summarize.py's to report, not the PDF's to
    fail on."""
    import language
    value = (provenance or {}).get("language") or os.environ.get(language.ENV_VAR)
    try:
        return language.normalize(value)
    except language.UnknownLanguage as exc:
        print(f"  warning: {exc}; using {language.DEFAULT}", file=sys.stderr)
        return language.DEFAULT


def _body_font(lang=None, provenance=None):
    """(font name, CSS stack, size factor) for the body text.

    The font is the document's own (provenance `font`), else this run's
    (PDF_FONT, from pipeline.sh --pdf-font), else PDF_FONT_<LANG>, else the
    built-in default — see fontchoice.chosen_font. PDF_FONT_FAMILY, the old
    whole-stack override, applies only when nothing chose a font: it names
    no one face, so there is nothing to size-match and the factor is 1.
    """
    lang = lang or _document_language(provenance)
    recorded = (provenance or {}).get("font")
    raw = os.environ.get("PDF_FONT_FAMILY")
    if raw and not recorded and not os.environ.get(fontchoice.RUN_ENV):
        parts = [p.strip() for p in raw.split(",") if p.strip()]
        return None, fontchoice.css_stack(parts), 1.0
    font = fontchoice.chosen_font(lang, recorded)
    return font, fontchoice.body_stack(font), fontchoice.size_factor(font)


def _font_stack(lang=None, provenance=None):
    return _body_font(lang, provenance)[1]


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
    """PDF_FRAMES: none (default), contact, or inline.

    `contact` keeps the citations as the model wrote them and collects the
    frames they name into a thumbnail appendix; `inline` replaces the first
    citation of each with the picture. Neither is the default: a keyframe is
    a screenshot of a video call, so most of them are a participant's face, a
    half-drawn slide or — the scene-change pass being what it is — solid
    black, and the sheet reads better without them. The citations themselves
    stay, so a reader can still scrub to the moment.
    """
    value = (os.environ.get("PDF_FRAMES") or "none").strip().lower()
    if value not in ("contact", "inline", "none"):
        print(f"  warning: PDF_FRAMES={value!r} — expected none, contact or "
              f"inline; using none", file=sys.stderr)
        return "none"
    return value


def transcript_mode():
    """PDF_TRANSCRIPT: none (default), hidden, or appendix.

    `hidden` writes the transcript into the page as white 1pt text between
    BEGIN_TRANSCRIPT / END_TRANSCRIPT markers: invisible to a reader, and
    still the first thing `pdftotext` hands an agent — at the cost of a few
    blank-looking pages at the back. See _hidden_transcript. Off by default
    since the markdown beside the PDF carries the transcript anyway.
    """
    value = (os.environ.get("PDF_TRANSCRIPT") or "none").strip().lower()
    if value not in ("hidden", "appendix", "none"):
        print(f"  warning: PDF_TRANSCRIPT={value!r} — expected none, hidden "
              f"or appendix; using none", file=sys.stderr)
        return "none"
    return value


def resources_mode():
    """PDF_RESOURCES: none (default) or appendix.

    `appendix` prints the slide images collected by --resources as Appendix
    B, captioned with the file each came from.
    """
    value = (os.environ.get("PDF_RESOURCES") or "none").strip().lower()
    if value not in ("appendix", "none"):
        print(f"  warning: PDF_RESOURCES={value!r} — expected none or "
              f"appendix; using none", file=sys.stderr)
        return "none"
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
    extensions = ["tables", "fenced_code", "sane_lists", "attr_list",
                  "nl2br", "md_in_html"]
    configs = {}
    if _have_pygments():
        # Colour spans for fenced code; without pygments the block is plain
        # text on the same dark panel.
        extensions.append("codehilite")
        configs["codehilite"] = {"css_class": "codehilite",
                                 "guess_lang": False, "use_pygments": True}
    return md.markdown(text, extensions=extensions,
                       extension_configs=configs, output_format="html5")


def _have_pygments():
    try:
        import pygments  # noqa: F401
    except ImportError:
        return False
    return True


# The editor theme for code blocks. One Dark: the colours of the operator's
# editor, readable in print, and its background is what the panel uses.
PYGMENTS_STYLE = "one-dark"


def _pygments_css():
    if not _have_pygments():
        return ""
    try:
        from pygments.formatters import HtmlFormatter
        return HtmlFormatter(style=PYGMENTS_STYLE).get_style_defs(".codehilite")
    except Exception:  # noqa: BLE001 - an unknown style is cosmetic
        return ""


# ---------------------------------------------------------------------------
# Callout boxes. The prompts write them as GitHub/Obsidian-style alerts:
#
#     > [!CONCEPT] Sifting property
#     > * ...
#
# which any Markdown reader shows as a quote, and which this turns into
# <div class="callout callout-concept"> before the conversion (md_in_html
# converts the inside as ordinary Markdown). It has to run before
# mathrender.extract: a display formula inside a quote is written with a
# "> " on every line, and extracted with them it is no longer LaTeX.
# A plain blockquote becomes a grey box with no title.
CALLOUT_START_RE = re.compile(r"^ {0,3}>\s?\[!([A-Za-z]+)\][+-]?[ \t]*(.*)$")
QUOTE_LINE_RE = re.compile(r"^ {0,3}>\s?(.*)$")
CALLOUT_KINDS = {
    "concept": "concept", "key": "concept", "definition": "concept",
    "tip": "concept", "summary": "concept", "abstract": "concept",
    "success": "concept", "formula": "concept",
    "example": "example", "demo": "example", "question": "example",
    "warning": "warning", "caution": "warning", "mistake": "warning",
    "attention": "warning",
    "important": "important", "exam": "important", "danger": "important",
    "remember": "important", "error": "important",
    "note": "note", "info": "note", "quote": "note", "todo": "note",
}
CALLOUT_DEFAULT_TITLES = {
    "concept": "Key concept", "example": "Example", "warning": "Watch out",
    "important": "Important", "note": "Note",
}


def _extract_callouts(text):
    """Turn top-level blockquotes into callout <div>s. See above."""
    lines = text.splitlines()
    out, i, fence = [], 0, None
    while i < len(lines):
        line = lines[i]
        f = FENCE_RE.match(line)
        if f and not line.startswith("    "):
            fence = None if fence == f.group(1) else (fence or f.group(1))
            out.append(line)
            i += 1
            continue
        if fence or not QUOTE_LINE_RE.match(line):
            out.append(line)
            i += 1
            continue
        group = []
        while i < len(lines) and QUOTE_LINE_RE.match(lines[i]):
            group.append(QUOTE_LINE_RE.match(lines[i]).group(1))
            i += 1
        start = CALLOUT_START_RE.match(line)
        if start:
            kind = CALLOUT_KINDS.get(start.group(1).lower(), "note")
            title = start.group(2).strip() or CALLOUT_DEFAULT_TITLES[kind]
            body = group[1:]
        else:
            kind, title, body = "quote", "", group
        inner = _extract_callouts("\n".join(body)).strip("\n")
        block = ["", f'<div class="callout callout-{kind}" markdown="1">']
        if title:
            block.append(f'<div class="callout-title" markdown="span">'
                         f'{title}</div>')
        block += ["", inner, "", "</div>", ""]
        out.extend(block)
    return "\n".join(out) + ("\n" if text.endswith("\n") else "")


# Fenced code gets a window bar with the language in it. python-markdown
# drops the language once pygments has used it, so the fence lines are read
# here, in order, and matched to the rendered blocks by position. Only
# unindented fences: those are the only ones fenced_code converts.
FENCE_OPEN_RE = re.compile(r"^(```+|~~~+)[ \t]*\{?\.?([\w+#.-]*)")
CODE_BLOCK_RE = re.compile(r'<div class="codehilite">(.*?)</div>', re.DOTALL)
PLAIN_PRE_RE = re.compile(r"<pre><code(?: class=\"language-([\w+#.-]+)\")?>")


def _fence_languages(text):
    langs, fence = [], None
    for line in text.splitlines():
        m = FENCE_OPEN_RE.match(line)
        if not m:
            continue
        if fence is None:
            fence = m.group(1)[0]
            langs.append(m.group(2))
        elif m.group(1)[0] == fence and not m.group(2):
            fence = None
    return langs


def _code_bar(lang):
    label = html.escape(lang) if lang else ""
    return ('<div class="code-bar"><span class="dot r"></span>'
            '<span class="dot y"></span><span class="dot g"></span>'
            f'<span class="lang">{label}</span></div>')


def _decorate_code(html_body, langs):
    """Wrap every code block in the editor-window chrome."""
    blocks = list(CODE_BLOCK_RE.finditer(html_body))
    if blocks:
        labels = langs if len(langs) == len(blocks) else [""] * len(blocks)
        out, last = [], 0
        for match, lang in zip(blocks, labels):
            out.append(html_body[last:match.start()])
            out.append(f'<div class="code-window">{_code_bar(lang)}'
                       f'<div class="codehilite">{match.group(1)}</div></div>')
            last = match.end()
        out.append(html_body[last:])
        return "".join(out)
    # No pygments: fenced_code's own <pre><code class="language-x">.
    if not PLAIN_PRE_RE.search(html_body):
        return html_body
    html_body = PLAIN_PRE_RE.sub(
        lambda m: (f'<div class="code-window">{_code_bar(m.group(1) or "")}'
                   f'<div class="codehilite"><pre><code>'), html_body)
    return html_body.replace("</code></pre>", "</code></pre></div></div>")


# Mathematical symbols typed straight into the prose — ω, ≤, ⇒, ∑, ², ℝ —
# rather than inside $...$. They are set in Computer Modern like the typeset
# maths, never in the body face. Greek, the arrow and operator blocks, the
# letter-like maths letters, super/subscripts, primes, × ÷ ± ¬.
MATH_SYMBOL_RE = re.compile(
    "[\u0391-\u03a9\u03b1-\u03c9\u03d1\u03d5\u03d6\u03f5"
    "\u2032-\u2037\u2070-\u209f\u00b2\u00b3\u00b9\u00d7\u00f7\u00b1\u00ac"
    "\u2102\u2107\u210b-\u2113\u2115\u2119-\u211d\u2124\u2128"
    "\u212c\u212d\u212f-\u2131\u2133-\u2138"
    "\u2190-\u21ff\u2200-\u22ff\u2308-\u230b\u27c0-\u27ff"
    "\u2900-\u2aff]+")
_TAG_RE = re.compile(r"(<[^>]*>)")
_SKIP_TAGS = ("pre", "code", "title", "style", "script")


def _wrap_math_symbols(html_body):
    """Put every run of maths symbols in the text into <span class="msym">."""
    out, skip = [], 0
    for piece in _TAG_RE.split(html_body):
        if piece.startswith("<"):
            m = re.match(r"<(/?)([a-zA-Z0-9]+)", piece)
            if m and m.group(2).lower() in _SKIP_TAGS:
                if m.group(1):
                    skip = max(0, skip - 1)
                elif not piece.endswith("/>"):
                    skip += 1
            out.append(piece)
        elif skip or not piece:
            out.append(piece)
        else:
            out.append(MATH_SYMBOL_RE.sub(
                lambda s: f'<span class="msym">{s.group(0)}</span>', piece))
    return "".join(out)


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
    text = _drop_legacy_header(text)
    text, links = _take_link_lines(text)
    if links:
        provenance["_links"] = links

    return text.strip(), "\n\n".join(transcripts), provenance


def _take_link_lines(text):
    """Lift the wrapper's link lines ("Youtube Link: `…`", "Clip: …") out of
    the body. They head the document, between the title and the first line
    of the model's own text; the PDF prints them in the title block, as
    plain grey lines, instead of as a paragraph of code chips."""
    lines, links, out, head = text.splitlines(), [], [], True
    for line in lines:
        if head and LINK_LINE_RE.match(line.strip()):
            links.append(line.strip())
            continue
        if head and line.strip() and not H1_RE.match(line):
            head = False
        out.append(line)
    return "\n".join(out), links


# What document.py used to put above the body, until 2026-09-13: a chapter
# placeholder for the operator to fill in, and the video's title as a fixed
# H1 over the model's own. Files written before then still carry them.
LEGACY_CHAPTER_LINE = "Chapter N — <topic> (<date>)"
LINK_LINE_RE = re.compile(
    r"^(?:(?:Youtube Link|Video Link|Source File|Clip)(?: \(Video \d+\))?:"
    r"|Summarized from \d+ videos as one\.)")
H1_RE = re.compile(r"^#\s+\S")


def _drop_legacy_header(text):
    """Strip the old wrapper's chapter placeholder and fixed video-title H1.

    The placeholder is never something a PDF should print. The video-title
    H1 goes only when the document has a second one right after the link
    lines — that is the model's own title, which is the one the sheet should
    carry, and it moves up to where the first one stood so the title still
    heads the page. A document with a single heading, old or new, is left
    exactly as it is.
    """
    lines = text.splitlines()
    lines = [ln for ln in lines if ln.strip() != LEGACY_CHAPTER_LINE]
    heads = [i for i, ln in enumerate(lines) if H1_RE.match(ln)]
    if len(heads) >= 2:
        first, second = heads[0], heads[1]
        between = [ln for ln in lines[first + 1:second] if ln.strip()]
        if all(LINK_LINE_RE.match(ln) for ln in between):
            lines[first] = lines[second]
            del lines[second]
    return "\n".join(lines)


LIST_ITEM_RE = re.compile(r"^(\s*)([-*+]|\d+[.)])\s+")
FENCE_RE = re.compile(r"^\s*(```|~~~)")


def _normalize_list_indent(text):
    """Re-indent nested lists so python-markdown nests them too.

    The models write sub-bullets two spaces in (Gemini always, Claude often),
    which GitHub, Obsidian and every other reader nest correctly. python-
    markdown nests only at four, and treats anything less as a continuation
    of the *parent* item — so a three-level outline flattened into one long
    list, which on a study sheet is the difference between structure and a
    wall of bullets. Each item's level is read from the indents seen so far
    and rewritten to four spaces a level; a continuation line (a paragraph or
    a display formula under an item) is indented to sit inside its item.
    Fenced code is left alone, and an outline already at four spaces is
    unchanged.
    """
    out, stack, fence = [], [], None
    for line in text.splitlines():
        f = FENCE_RE.match(line)
        if f:
            fence = None if fence == f.group(1) else (fence or f.group(1))
            out.append(line)
            continue
        if fence or not line.strip():
            out.append(line)
            continue
        expanded = line.expandtabs(4)
        indent = len(expanded) - len(expanded.lstrip(" "))
        item = LIST_ITEM_RE.match(expanded)
        if item:
            if not stack:
                if indent >= 4:
                    # An indented list with no list open is markdown's
                    # code block; not ours to reinterpret.
                    out.append(line)
                    continue
                if out and out[-1].strip():
                    # "The modules are:" straight into "1. Signals" — a list
                    # to CommonMark and to the model, but python-markdown
                    # needs the blank line or it prints numbered prose.
                    out.append("")
                stack = [indent]
            elif indent > stack[-1]:
                stack.append(indent)
            else:
                while len(stack) > 1 and indent < stack[-1]:
                    stack.pop()
            level = len(stack) - 1
            out.append(" " * (4 * level) + expanded.lstrip(" "))
            continue
        if stack and indent > stack[0]:
            # Continuation of the deepest item whose indent it reaches.
            level = max(i for i, w in enumerate(stack) if w <= indent)
            out.append(" " * (4 * (level + 1)) + expanded.lstrip(" "))
            continue
        stack = []
        out.append(line)
    return "\n".join(out) + ("\n" if text.endswith("\n") else "")


# "(Video 1, Frame 52 @ 0:08:52)", "(Frame 280 @ Video 1 [02:21:00])",
# "(Frames 20–26 @ 0:05:25–0:05:41)", and the frameless "(Video 6,
# [02:51:30])": the parenthesised citation as a whole, on the rendered HTML,
# so the <em> markdown wraps it in is outside the span.
CITATION_RE = re.compile(
    r"\(\s*(?:video\s*\d+\s*,\s*)?frames?\s*#?\s*\d+[^()<>\n]*\)"
    r"|\(\s*video\s*\d+\s*,[^()<>\n]*\)",
    re.IGNORECASE)


def _fade_citations(html_body):
    """Wrap every frame citation so the stylesheet can fade it."""
    return CITATION_RE.sub(
        lambda m: f'<span class="cite">{m.group(0)}</span>', html_body)


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
    ordered = sorted(frames, key=lambda f: (getattr(f, "part", 0),
                                             f.timestamp_s))
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
            # Non-zero on a --combine render: the timestamp is relative to
            # that video, and the caption has to say which one.
            "part": getattr(frame, "part", 0),
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


# Design tokens — DESIGN.md is the specification; keep the two in step.
INK = "#1b1f27"
MUTED = "#5f6875"
FAINT = "#8a93a0"
HAIRLINE = "#d3d9e1"
NAVY = "#1f3a5f"
NAVY_INK = "#16233a"
LINK = "#2d5b8f"
TABLE_HEAD = "#e9eef5"
# (fill, rule, title) per callout kind.
CALLOUT_COLOURS = {
    "concept": ("#edf5ef", "#2f7d4f", "#1e6a3d"),
    "example": ("#edf3fa", "#2f6aa3", "#1f578c"),
    "warning": ("#fcf4e5", "#c7811f", "#8a5810"),
    "important": ("#fcefef", "#b3373b", "#9c2b30"),
    "note": ("#f4f6f8", "#9aa4b1", "#46505d"),
    "quote": ("#f4f6f8", "#9aa4b1", "#46505d"),
}
CODE_BG = "#1e2229"
CODE_BAR = "#2b313b"
CODE_INK = "#e3e7ee"
# Thai needs the taller line: its vowels and tone marks stack above and
# below the letters.
LINE_HEIGHT = {"th": 1.62, "en": 1.42}


def _css(lang=None, provenance=None):
    lang = lang or _document_language(provenance)
    nominal = _font_size()
    _font, stack, factor = _body_font(lang, provenance)
    body_pt = nominal * factor
    # Relative to the body, so they follow it into headings and tables:
    # maths at Computer Modern's nominal size, code at the x-height of the
    # text (JetBrains Mono's letters are taller than CM's at the same size).
    math_em = 1.0 / factor
    mono_em = fontchoice.mono_factor() / factor
    mono = fontchoice.css_stack(fontchoice.MONO_STACK)
    maths = fontchoice.css_stack(fontchoice.MATH_STACK)
    callouts = "\n".join(
        f".callout-{kind} {{ background: {fill}; border-left-color: {rule}; }}\n"
        f".callout-{kind} .callout-title {{ color: {title}; }}"
        for kind, (fill, rule, title) in CALLOUT_COLOURS.items())
    return f"""
@page {{
    size: {_page_size()};
    margin: 16mm 15mm 18mm 15mm;
    @bottom-left {{
        content: string(doctitle);
        font-family: {stack};
        font-size: {7 * factor:.2f}pt;
        color: {FAINT};
    }}
    @bottom-right {{
        content: counter(page) " / " counter(pages);
        font-family: {stack};
        font-size: {7 * factor:.2f}pt;
        color: {FAINT};
    }}
}}
body {{
    font-family: {stack};
    font-size: {body_pt:.2f}pt;
    line-height: {LINE_HEIGHT.get(lang, 1.45)};
    color: {INK};
}}

/* Title block */
h1 {{ string-set: doctitle content(); font-size: 2.0em; font-weight: 700;
      color: {NAVY_INK}; margin: 0 0 3pt 0; line-height: 1.2; }}
.docmeta {{ font-size: 0.95em; color: {MUTED}; margin: 0 0 1pt 0; }}
.source {{ font-size: 0.85em; color: {MUTED}; margin: 0 0 1pt 0;
           word-break: break-all; }}
.source:last-of-type, .source.last {{ margin-bottom: 12pt; }}
.source a {{ color: {MUTED}; text-decoration: none; }}

/* Sections: a navy banner, like the exercise sheet's */
h2 {{ background: {NAVY}; color: #ffffff; font-size: 1.2em; font-weight: 700;
      padding: 4pt 9pt; border-radius: 3pt; margin: 16pt 0 8pt 0;
      line-height: 1.35; break-after: avoid; }}
h3 {{ font-size: 1.1em; font-weight: 700; color: {NAVY_INK};
      margin: 11pt 0 4pt 0; break-after: avoid; }}
h4 {{ font-size: 1em; font-weight: 700; color: #3a4452;
      margin: 8pt 0 3pt 0; break-after: avoid; }}

/* Running text */
p {{ margin: 0 0 5pt 0; orphans: 2; widows: 2; }}
ul, ol {{ margin: 2pt 0 6pt 0; padding-left: 15pt; }}
li {{ margin: 1.5pt 0; orphans: 2; widows: 2; }}
li > ul, li > ol {{ margin: 1pt 0 2pt 0; }}
strong {{ font-weight: 700; color: #111722; }}
a {{ color: {LINK}; text-decoration: none; }}
/* The banners separate the sections; a --- between them would be a second
   rule under the first. */
hr {{ border: none; margin: 4pt 0; }}

/* Tables */
table {{ border-collapse: collapse; width: 100%; margin: 6pt 0 9pt 0;
         font-size: 0.93em; }}
th, td {{ border: 0.6pt solid {HAIRLINE}; padding: 3.5pt 6pt; text-align: left;
          vertical-align: top; }}
th {{ background: {TABLE_HEAD}; font-weight: 700; color: {NAVY_INK}; }}
tr {{ break-inside: avoid; }}

/* Callout boxes: a tinted panel with a rule down the left */
.callout {{ margin: 8pt 0 10pt 0; padding: 6pt 10pt 4pt 10pt;
            border-left: 3pt solid; border-radius: 0 3pt 3pt 0; }}
.callout-title {{ font-weight: 700; margin: 0 0 3pt 0; }}
.callout p:last-child, .callout ul:last-child, .callout ol:last-child,
.callout table:last-child {{ margin-bottom: 2pt; }}
.callout table {{ background: #ffffff; }}
{callouts}
/* The must-remember box is framed all round, as on the sheet. */
.callout-important {{ border: 0.8pt solid #deaaac; border-left: 3pt solid #b3373b;
                      border-radius: 3pt; }}
.callout-quote {{ font-style: normal; }}

/* Code: an editor window — dark panel, window bar, JetBrains Mono */
code {{ font-family: {mono}; font-size: {mono_em:.3f}em;
        background: #262b34; color: {CODE_INK};
        padding: 0.4pt 3pt; border-radius: 2.5pt; }}
.code-window {{ margin: 7pt 0 10pt 0; background: {CODE_BG};
                border-radius: 5pt; break-inside: avoid; }}
.code-bar {{ background: {CODE_BAR}; border-radius: 5pt 5pt 0 0;
             padding: 3pt 8pt; font-family: {mono};
             font-size: {mono_em * 0.85:.3f}em; color: #9aa4b2;
             line-height: 1.3; }}
.code-bar .dot {{ display: inline-block; width: 5.5pt; height: 5.5pt;
                  border-radius: 2.75pt; margin-right: 3pt; }}
.code-bar .r {{ background: #ff5f57; }}
.code-bar .y {{ background: #febc2e; }}
.code-bar .g {{ background: #28c840; }}
.code-bar .lang {{ float: right; }}
{_pygments_css()}
.codehilite {{ background: {CODE_BG}; border-radius: 0 0 5pt 5pt; }}
.codehilite pre {{ margin: 0; padding: 7pt 10pt 8pt 10pt; background: {CODE_BG};
                   color: {CODE_INK}; font-family: {mono};
                   font-size: {mono_em:.3f}em; line-height: 1.45;
                   white-space: pre-wrap; word-wrap: break-word; }}
pre code {{ font-size: 1em; background: none; padding: 0; border-radius: 0;
            color: inherit; }}

/* Maths. Typeset formulas are SVG sized in points by mathrender; symbols
   typed into the prose (ω, ≤, ⇒) are set in Computer Modern too. */
img.math {{ margin: 0; }}
.math-block {{ display: block; text-align: center; margin: 7pt 0;
               break-inside: avoid; }}
.math-line {{ display: block; margin: 2pt 0; }}
.math-fallback {{ font-family: {maths}; font-style: italic;
                  font-size: {math_em:.3f}em; }}
.msym {{ font-family: {maths}; font-size: {math_em:.3f}em; }}

/* Small print */
.colophon {{ margin-top: 16pt; padding-top: 4pt;
             border-top: 0.6pt solid {HAIRLINE};
             font-size: 0.78em; color: {FAINT}; }}
.notes {{ font-size: 0.9em; color: #8a6d3b; }}
/* Frame citations, in older documents: kept, faded (70% transparent). */
.cite {{ opacity: 0.3; }}

/* Appendices */
.appendix {{ break-before: page; }}
.transcript {{ font-size: 0.9em; line-height: 1.45; color: #333;
               white-space: pre-wrap; }}
figure.frame {{ margin: 10pt 0; text-align: center; break-inside: avoid; }}
figure.frame img {{ max-width: 100%; max-height: 105mm;
                    border: 1px solid {HAIRLINE}; border-radius: 3px; }}
figure.frame figcaption {{ font-size: 0.9em; color: #666; margin-top: 3pt; }}
/* Contact sheet: three thumbnails to a row, inline-block rather than grid
   because that lays out identically on every WeasyPrint version we might
   meet on the box. */
.contact {{ margin-top: 6pt; }}
figure.thumb {{ display: inline-block; width: 31.5%; margin: 0 1% 8pt 0;
                text-align: center; vertical-align: top;
                break-inside: avoid; }}
figure.thumb img {{ width: 100%; border: 1px solid {HAIRLINE};
                    border-radius: 2px; }}
figure.thumb figcaption {{ font-size: 0.85em; color: #666; margin-top: 2pt; }}

/* The transcript layer. White on white at 1pt: no reader sees it, every text
   extractor gets it. Not display:none — WeasyPrint would then put nothing in
   the PDF at all, which is the opposite of the point. */
.hidden-transcript {{ color: #ffffff; font-size: 1pt; line-height: 1pt;
                      letter-spacing: 0; word-spacing: 0;
                      overflow-wrap: anywhere; margin: 0; }}
.hidden-next {{ break-before: page; }}
"""


# What the subtitle calls the document, by prompt. English whatever the
# summary language is, like the rest of the wrapper (see language.py).
KIND_LABELS = {
    "lecture": "Lecture notes", "tutorial": "Tutorial guide",
    "meeting": "Meeting summary", "video": "Video summary",
}


def _doc_kind_label(doc_kind, provenance):
    name = (doc_kind or provenance.get("prompt") or "").lower()
    name = Path(name).stem if name else ""
    for key, label in KIND_LABELS.items():
        if name.startswith(key):
            return label
    return "Summary"


def _link_line_html(line):
    """One wrapper link line, backticks dropped and the URL made a link."""
    text = html.escape(line.replace("`", ""))
    return re.sub(r"(https?://[^\s<]+)", r'<a href="\1">\1</a>', text)


def _subtitle_html(provenance, doc_kind, source):
    """The grey lines under the title: what this is, when, and from where."""
    generated = provenance.get("generated") or date.today().isoformat()
    bits = [_doc_kind_label(doc_kind, provenance), generated]
    if provenance.get("clip"):
        bits.append(f"clip {provenance['clip']}")
    if provenance.get("videos"):
        bits.append(f"{provenance['videos']} videos")
    parts = [f'<p class="docmeta subtitle">'
             f'{" · ".join(html.escape(str(b)) for b in bits)}</p>']
    links = provenance.get("_links") or []
    if links:
        parts += [f'<p class="source">{_link_line_html(ln)}</p>'
                  for ln in links]
        parts[-1] = parts[-1].replace('class="source"', 'class="source last"')
    elif source:
        src = html.escape(str(source))
        link = (f'<a href="{src}">{src}</a>'
                if re.match(r"https?://", str(source)) else src)
        parts.append(f'<p class="source">Source: {link}</p>')
    return "".join(parts)


def _colophon_html(provenance):
    """The small print at the end: which model wrote it, for which run."""
    bits = []
    for key, label in (("model", "model"), ("prompt", "prompt"),
                       ("run_id", "run"), ("font", "font")):
        if provenance.get(key):
            bits.append(f"{label}: {html.escape(str(provenance[key]))}")
    if not bits:
        return ""
    return ('<p class="colophon">Generated by meeting-transcriber · '
            + " · ".join(bits) + "</p>")


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
        if info.get("part"):
            caption = (f"Frame {number} — Video {info['part']}, "
                       f"{_fmt_timestamp(info['timestamp'])}")
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
           max_width=None, doc_kind=None):
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
        lang = _document_language(provenance)

        # The document's own "# Title" line is the PDF title — the model's,
        # since 2026-09-13 — and stays in the body as the heading. The
        # caller's title (the video's) is only the fallback for a body with
        # no heading of its own.
        m = re.search(r"^#\s+(.+)$", body_md, re.MULTILINE)
        doc_title = (m.group(1).strip() if m else None) or title \
            or provenance.get("title") or "Summary"

        slide_images = collect_slide_images(resources)

        # Callouts first: a formula inside a quote carries "> " on every line
        # until the quote markers are gone. See _extract_callouts.
        body_md = _extract_callouts(body_md)
        # Maths comes out of the markdown *before* the HTML conversion: python-
        # markdown would eat the underscores and backslashes otherwise. It goes
        # back in after the citation passes, so those never have to step over a
        # base64 data: URI. See mathrender.
        body_md, math_exprs = mathrender.extract(body_md)
        # After extract: a display formula under a bullet is one token by
        # now, so it re-indents as one line instead of being cut mid-matrix.
        body_md = _normalize_list_indent(body_md)

        body_html = _markdown_to_html(body_md)
        body_html = _decorate_code(body_html, _fence_languages(body_md))

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

        body_html = _fade_citations(body_html)
        # Before the maths goes back in: its tokens are plain letters, and a
        # restored formula is an <img> whose data: URI must not be scanned.
        body_html = _wrap_math_symbols(body_html)
        body_html = mathrender.restore(
            body_html,
            mathrender.render_all(math_exprs, size_pt=_font_size()))

        # What it is, when and from where go under the title, not over it:
        # this is a study sheet, and the first thing on the page is what it
        # is about. Which model and run made it is small print at the end.
        meta = _subtitle_html(provenance, doc_kind,
                              source or provenance.get("source"))
        if "</h1>" in body_html:
            body_html = body_html.replace("</h1>", "</h1>" + meta, 1)
        else:
            body_html = meta + body_html

        t_mode = transcript_mode()
        document = (
            f"<html><head><meta charset='utf-8'>"
            f"<title>{html.escape(doc_title)}</title></head><body>"
            f"{body_html}"
            f"{_colophon_html(provenance)}"
            f"{frame_appendix}"
            f"{_appendix_resources(resources, slide_images) if resources_mode() == 'appendix' else ''}"
            f"{_appendix_transcript(transcript) if t_mode == 'appendix' else ''}"
            f"{_hidden_transcript(transcript) if t_mode == 'hidden' else ''}"
            f"</body></html>"
        )

        try:
            HTML(string=document, base_url=str(output_path.parent)).write_pdf(
                str(output_path),
                stylesheets=[CSS(string=_css(lang, provenance))])
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


def load_manifest_frames(manifest_path, part=0):
    """Read one frames manifest into FrameMeta objects.

    `part` tags every frame with the video it came from, for a --combine
    render; the numbering is left to the caller — see load_part_manifests —
    because it has to run once over *every* video's frames together, exactly
    the way llm_client.assign_numbers demands for a single recording.
    Numbering here, per manifest, is how two videos both end up with a
    "Frame 4" that name different pictures.
    """
    import json
    from llm_client import FrameMeta

    data = json.loads(Path(manifest_path).read_text())
    return [FrameMeta(timestamp_s=e["timestamp_s"], kind=e["kind"],
                      path=e["path"], part=part)
            for e in data.get("frames", [])]


def load_part_manifests(manifest_paths):
    """Frames for several videos summarized as one document, numbered once.

    The Nth manifest's frames are tagged part N (1-based) and the whole list
    is numbered globally in (part, timestamp) order, so the model is shown —
    and the PDF resolves — one number per picture across all the videos. An
    entry may be None or "" for a video with no frames; it still counts as a
    part so the numbering of the videos after it stays right.
    """
    from llm_client import assign_numbers

    frames = []
    for part, path in enumerate(manifest_paths, start=1):
        if not path:
            continue
        frames.extend(load_manifest_frames(path, part=part))
    return assign_numbers(frames)


def want_pdf():
    return (os.environ.get("SUMMARY_WRITE_PDF", "1").strip().lower()
            not in ("0", "false", "no"))


def want_markdown():
    return (os.environ.get("SUMMARY_WRITE_MARKDOWN", "1").strip().lower()
            not in ("0", "false", "no"))


def _main(argv):
    """CLI: `pdf.py <summary.md> <out.pdf> [--frames-manifest P]... [--work-dir D]`.

    --frames-manifest may be repeated to re-render a --combine document: the
    Nth manifest is video N's, in the same order the summary was made from,
    and "-" holds the place of a video that had no frames.

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
            opts.setdefault(a, []).append(
                argv[i + 2] if i + 2 < len(argv) else None)
            skip = True
        elif any(a.startswith(v + "=") for v in _VALUED):
            k, v = a.split("=", 1)
            opts.setdefault(k, []).append(v)
        elif not a.startswith("--"):
            positional.append(a)
    manifests = [m for m in opts.get("--frames-manifest", []) if m is not None]
    work_dir = (opts.get("--work-dir") or [None])[-1]
    args = positional
    if len(args) < 2:
        print("Usage: pdf.py <summary.md> <out.pdf> [--frames-manifest PATH] "
              "[--work-dir DIR]", file=sys.stderr)
        return 2

    if len(manifests) > 1:
        frames = load_part_manifests(
            [None if m in ("-", "") else m for m in manifests])
    elif manifests and manifests[0] not in ("-", ""):
        from llm_client import assign_numbers
        frames = assign_numbers(load_manifest_frames(manifests[0]))
    else:
        frames = []
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
