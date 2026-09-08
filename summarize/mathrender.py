#!/usr/bin/env python3
"""
LaTeX in a summary, typeset into the PDF.

The model writes maths the way it writes everything else: inline as $L/R$ and
display as $$d_{\\text{nodal}} = \\dots$$. python-markdown passes that through
untouched, so before this module the PDF printed the source — literal
backslashes, braces and all — while markdown readers rendered it fine.

WeasyPrint has no JavaScript engine (so no KaTeX or MathJax) and no MathML
support, so the only way to get typeset maths onto the page is to hand it a
picture. matplotlib's `mathtext` is exactly that: a self-contained typesetter
for a large LaTeX subset that ships **Computer Modern** — the TeX face — as
`mathtext.fontset = "cm"`, and needs no TeX installation, no node, no network.
Each expression becomes a small SVG inlined as a `data:` URI.

Four things are worth knowing before editing this:

  * **Extraction runs on the markdown, before the HTML conversion.** Markdown
    eats the syntax otherwise: `_{trans}` becomes emphasis, backslashes vanish,
    and `$$` blocks get wrapped in paragraphs mid-expression. So the maths is
    lifted out first and put back after, keyed by an opaque alphanumeric token
    that markdown has no reason to touch.

  * **Baseline alignment is computed, not guessed.** MathTextParser reports
    width, height and *depth* (how far the expression hangs below its
    baseline); depth becomes a negative `vertical-align`, so inline maths sits
    on the text baseline instead of floating.

  * **Nothing here may fail the render.** matplotlib is optional and its
    parser rejects plenty of real LaTeX (`\\begin{cases}`, `\\substack`), so
    every failure degrades to cleaned-up text in a serif face. A summary with
    ugly maths still beats no PDF — the same rule the rest of the export
    follows.

  * **`\\begin{aligned}` is split here, not in mathtext.** mathtext has no
    environments at all; multi-row display maths is broken on `\\\\` and
    rendered a row at a time, stacked. That covers what the lecture prompts
    actually produce; anything else falls back to text.
"""
import base64
import html as _html
import io
import os
import re

# The SVG's internal coordinate scale only. Point sizes are computed back out
# of it, so this number never reaches the page.
DPI = 100.0

# Computer Modern's x-height is small next to a UI sans at the same nominal
# size, so maths set at the body size reads a size too small. Nudge it up.
DEFAULT_MATH_SCALE = 1.15

# Regions where a `$` is a dollar sign, not maths.
CODE_SPAN_RE = re.compile(r"```.*?```|~~~.*?~~~|`[^`\n]*`", re.DOTALL)

DISPLAY_RE = re.compile(r"\$\$(.+?)\$\$", re.DOTALL)
# Inline maths: no newline inside, no space just inside either delimiter (so
# "$5 and $6" doesn't match), and not adjacent to another '$'.
INLINE_RE = re.compile(r"(?<![\$\\])\$(?!\s)([^\$\n]+?)(?<![\s\\])\$(?!\$)")

TOKEN_PREFIX = "MTHX"
TOKEN_SUFFIX = "Z"
TOKEN_RE = re.compile(rf"{TOKEN_PREFIX}(\d+){TOKEN_SUFFIX}")

# The environments mathtext cannot parse but that split cleanly into rows.
ENV_RE = re.compile(
    r"\\begin\{(aligned|align\*?|gather\*?|split|array)\}"
    r"(?:\{[^}]*\})?(.*?)\\end\{\1\}", re.DOTALL)
ROW_SPLIT_RE = re.compile(r"\\\\(?:\s*\[[^\]]*\])?")

# Groups whose digits are already upright and must not be rewritten.
_TEXT_COMMANDS = ("\\text", "\\mathrm", "\\mathbf", "\\mathit", "\\mathsf",
                  "\\mathtt", "\\operatorname", "\\textbf", "\\textit")

_engine_cache = "unset"


def _token(index):
    return f"{TOKEN_PREFIX}{index}{TOKEN_SUFFIX}"


def _protected_ranges(text):
    return [m.span() for m in CODE_SPAN_RE.finditer(text)]


def _inside(ranges, pos):
    return any(start <= pos < end for start, end in ranges)


def extract(markdown_text):
    """Lift every $…$ / $$…$$ out of the markdown, leaving opaque tokens.

    Returns `(text_with_tokens, exprs)` where each expr is
    `{"tex": ..., "display": bool}` and expr *i* corresponds to token *i*.
    Maths inside code spans and fenced blocks is left alone: in a lecture
    summary those are terminal transcripts and ASCII diagrams, where `$` is a
    shell prompt.
    """
    exprs = []

    def _replace(pattern, display, text):
        ranges = _protected_ranges(text)

        def _sub(match):
            if _inside(ranges, match.start()):
                return match.group(0)
            tex = match.group(1).strip()
            if not tex:
                return match.group(0)
            if not display and _looks_like_money(tex):
                return match.group(0)
            exprs.append({"tex": tex, "display": display})
            return _token(len(exprs) - 1)

        return pattern.sub(_sub, text)

    text = _replace(DISPLAY_RE, True, markdown_text)
    text = _replace(INLINE_RE, False, text)
    return text, exprs


def _looks_like_money(tex):
    """"$5 and change $" — prose between two currency amounts, not maths.

    Anything with a backslash, a sub/superscript or a brace is maths. What is
    left is caught by the shape of the rest: starts with a digit and then runs
    on in words.
    """
    if any(c in tex for c in "\\_^{}"):
        return False
    return bool(re.match(r"^\d", tex)) and bool(re.search(r"\s\w", tex))


def restore(html_text, snippets):
    """Put the rendered snippets back where their tokens are.

    A display token that markdown left alone in its own paragraph replaces the
    whole `<p>`, so a centred block never has to live inside one.
    """
    for index, snippet in enumerate(snippets):
        token = _token(index)
        html_text = re.sub(rf"<p>\s*{token}\s*</p>", lambda _m, s=snippet: s,
                           html_text)
        html_text = html_text.replace(token, snippet)
    return html_text


def available():
    """True when matplotlib is importable, i.e. maths will be typeset."""
    return _engine() is not None


def _engine():
    """The matplotlib mathtext renderer, or None. Imported once, lazily."""
    global _engine_cache
    if _engine_cache != "unset":
        return _engine_cache
    try:
        import matplotlib
        matplotlib.use("Agg")
        from matplotlib import rcParams
        from matplotlib.figure import Figure
        from matplotlib.font_manager import FontProperties
        from matplotlib.mathtext import MathTextParser
    except Exception:  # noqa: BLE001 - any import problem means "no maths"
        _engine_cache = None
        return None

    rcParams["mathtext.fontset"] = (
        os.environ.get("PDF_MATH_FONTSET") or "cm").strip() or "cm"
    rcParams["mathtext.default"] = "it"
    parser = MathTextParser("path")

    def _render(tex, size_pt, color):
        """-> (svg_bytes, width_pt, height_pt, depth_pt). Raises on bad TeX."""
        prop = FontProperties(size=size_pt)
        parsed = parser.parse(f"${tex}$", dpi=DPI, prop=prop)
        width, height, depth = parsed.width, parsed.height, parsed.depth
        # Half a pixel of padding a side, to catch the italic overhang that
        # mathtext's reported box occasionally cuts. It is deliberately small:
        # the padding is layout whitespace once the image is inline, and a
        # whole pixel a side put a visible gap inside every "($d$)".
        pad = 0.5
        box_w, box_h = width + 2 * pad, height + 2 * pad
        fig = Figure(figsize=(box_w / DPI, box_h / DPI), dpi=DPI)
        fig.text(pad / box_w, (depth + pad) / box_h, f"${tex}$",
                 fontsize=size_pt, color=color, va="baseline", ha="left")
        buf = io.BytesIO()
        fig.savefig(buf, format="svg", transparent=True, pad_inches=0)
        pt = 72.0 / DPI
        return buf.getvalue(), box_w * pt, box_h * pt, (depth + pad) * pt

    _engine_cache = _render
    return _engine_cache


def scale_from_env():
    try:
        value = float(os.environ.get("PDF_MATH_SCALE", "") or
                      DEFAULT_MATH_SCALE)
    except ValueError:
        return DEFAULT_MATH_SCALE
    return value if 0.5 <= value <= 3.0 else DEFAULT_MATH_SCALE


def enabled():
    """PDF_MATH=1 (default) renders maths; 0 leaves the LaTeX as text."""
    return (os.environ.get("PDF_MATH", "1").strip().lower()
            not in ("0", "false", "no"))


def render_all(exprs, *, size_pt=8.0, color="#16181d", scale=None):
    """Render every expression to an HTML snippet, in order.

    Identical expressions are rendered once — a lecture that writes $L$ forty
    times pays for one SVG.
    """
    if scale is None:
        scale = scale_from_env()
    size = size_pt * scale
    engine = _engine() if enabled() else None
    cache = {}
    out = []
    for expr in exprs:
        key = (expr["tex"], expr["display"])
        if key not in cache:
            cache[key] = _one(expr["tex"], expr["display"], engine, size, color)
        out.append(cache[key])
    return out


def _one(tex, display, engine, size_pt, color):
    rows = _rows(tex) if display else [_flatten(tex)]
    pieces = []
    for row in rows:
        row = row.strip()
        if not row:
            continue
        pieces.append(_row_html(row, engine, size_pt, color, display))
    if not pieces:
        return ""
    if not display:
        return pieces[0]
    lines = "".join(f'<span class="math-line">{p}</span>' for p in pieces)
    return f'<span class="math-block">{lines}</span>'


def _row_html(row, engine, size_pt, color, display):
    prepared = _prepare(row)
    if engine is not None:
        for candidate in (prepared, _compat(_flatten(row))):
            # Two attempts: the tidied form, then the author's own. The digit
            # rewriting below is cosmetic, so it must never be what loses an
            # expression that mathtext would otherwise have accepted.
            try:
                svg, width, height, depth = engine(candidate, size_pt, color)
            except Exception:  # noqa: BLE001 - unparseable TeX, keep going
                continue
            cls = "math math-display" if display else "math math-inline"
            uri = ("data:image/svg+xml;base64,"
                   + base64.b64encode(svg).decode("ascii"))
            return (f'<img class="{cls}" src="{uri}" '
                    f'alt="{_html.escape(row, quote=True)}" '
                    f'style="width:{width:.2f}pt;height:{height:.2f}pt;'
                    f'vertical-align:{-depth:.2f}pt" />')
    return _fallback_html(row)


def _rows(tex):
    """Split display maths into rows: aligned/gather environments, or `\\\\`."""
    match = ENV_RE.search(tex)
    body = match.group(2) if match else tex
    return [_flatten(r) for r in ROW_SPLIT_RE.split(body)]


def _flatten(tex):
    """One line, no alignment marks — mathtext has neither concept."""
    return re.sub(r"\s+", " ", tex.replace("&", " ")).strip()


# Spellings mathtext doesn't know, and the equivalent it does. Cheaper than
# losing the whole expression to the text fallback over one arrow.
_COMPAT = {
    "\\implies": "\\Rightarrow", "\\impliedby": "\\Leftarrow",
    "\\iff": "\\Leftrightarrow", "\\dfrac": "\\frac",
    "\\tfrac": "\\frac", "\\operatorname": "\\mathrm",
    "\\mbox": "\\mathrm", "\\textbf": "\\mathbf",
    "\\textit": "\\mathit", "\\lvert": "|", "\\rvert": "|",
    "\\nonumber": "", "\\notag": "", "\\limits": "",
}


def _compat(tex):
    """Rewrite LaTeX mathtext doesn't implement into what it does."""
    def _sub(match):
        return _COMPAT.get(match.group(0), match.group(0))
    return re.sub(r"\\[A-Za-z]+", _sub, tex)


def _prepare(tex):
    return _upright_digits(_compat(_flatten(tex)))


def _upright_digits(tex):
    """Wrap bare digit runs in \\mathrm{}, the way LaTeX sets them.

    matplotlib's `mathtext.default = "it"` italicises digits as well as
    variables, so `2 \\times 10^8` comes out in a slanted face that reads as
    wrong to anyone who knows TeX. Digits already inside a \\text{}-style group
    are left exactly as written.
    """
    out = []
    i = 0
    depth = 0
    text_groups = []      # brace depths at which an upright group opened
    pending_text = False
    while i < len(tex):
        ch = tex[i]
        if ch == "\\":
            command = re.match(r"\\[A-Za-z]+", tex[i:])
            if command:
                name = command.group(0)
                out.append(name)
                i += len(name)
                pending_text = name in _TEXT_COMMANDS
                continue
            out.append(tex[i:i + 2])
            i += 2
            pending_text = False
            continue
        if ch == "{":
            depth += 1
            if pending_text:
                text_groups.append(depth)
                pending_text = False
            out.append(ch)
            i += 1
            continue
        if ch == "}":
            if text_groups and text_groups[-1] == depth:
                text_groups.pop()
            depth -= 1
            out.append(ch)
            i += 1
            continue
        if ch.isdigit() and not text_groups:
            run = re.match(r"\d+(?:\.\d+)?", tex[i:]).group(0)
            out.append("\\mathrm{" + run + "}")
            i += len(run)
            pending_text = False
            continue
        if not ch.isspace():
            pending_text = False
        out.append(ch)
        i += 1
    return "".join(out)


_FALLBACK_SYMBOLS = {
    r"\times": "\u00d7", r"\cdot": "\u00b7", r"\approx": "\u2248",
    r"\neq": "\u2260", r"\leq": "\u2264", r"\geq": "\u2265",
    r"\le": "\u2264", r"\ge": "\u2265", r"\pm": "\u00b1",
    r"\to": "\u2192", r"\rightarrow": "\u2192", r"\Rightarrow": "\u21d2",
    r"\dots": "\u2026", r"\ldots": "\u2026", r"\cdots": "\u22ef",
    r"\infty": "\u221e", r"\sum": "\u03a3", r"\prod": "\u03a0",
    r"\alpha": "\u03b1", r"\beta": "\u03b2", r"\gamma": "\u03b3",
    r"\delta": "\u03b4", r"\lambda": "\u03bb", r"\mu": "\u03bc",
    r"\sigma": "\u03c3", r"\tau": "\u03c4", r"\theta": "\u03b8",
    r"\Delta": "\u0394", r"\in": "\u2208", r"\ll": "\u226a", r"\gg": "\u226b",
    r"\quad": " ", r"\qquad": "  ", r"\,": " ", r"\;": " ", r"\!": "",
    r"\left": "", r"\right": "", r"\ ": " ",
}


def _fallback_html(tex):
    """Readable text for maths that couldn't be typeset. Escaped, never raw."""
    text = tex
    text = re.sub(r"\\(?:text|mathrm|mathbf|mathit|operatorname)\{([^{}]*)\}",
                  r"\1", text)
    text = re.sub(r"\\frac\{([^{}]*)\}\{([^{}]*)\}", r"(\1)/(\2)", text)
    for name in sorted(_FALLBACK_SYMBOLS, key=len, reverse=True):
        text = text.replace(name, _FALLBACK_SYMBOLS[name])
    text = re.sub(r"\\[A-Za-z]+", lambda m: m.group(0)[1:], text)
    escaped = _html.escape(_flatten(text))
    escaped = re.sub(r"_\{([^{}]*)\}", r"<sub>\1</sub>", escaped)
    escaped = re.sub(r"\^\{([^{}]*)\}", r"<sup>\1</sup>", escaped)
    escaped = re.sub(r"_(\w)", r"<sub>\1</sub>", escaped)
    escaped = re.sub(r"\^(\w)", r"<sup>\1</sup>", escaped)
    escaped = escaped.replace("{", "").replace("}", "")
    return f'<span class="math-fallback">{escaped}</span>'
