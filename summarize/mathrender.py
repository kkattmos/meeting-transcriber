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

Five things are worth knowing before editing this:

  * **Extraction runs on the markdown, before the HTML conversion.** Markdown
    eats the syntax otherwise: `_{trans}` becomes emphasis, backslashes vanish,
    and `$$` blocks get wrapped in paragraphs mid-expression. So the maths is
    lifted out first and put back after, keyed by an opaque alphanumeric token
    that markdown has no reason to touch.

  * **Baseline alignment is computed, not guessed.** MathTextParser reports
    width, height and *depth* (how far the expression hangs below its
    baseline); depth becomes a negative `vertical-align`, so inline maths sits
    on the text baseline instead of floating.

  * **Environments are composed here, not parsed by mathtext.** mathtext has
    no `\\begin` at all, and `\\begin{cases}` / `\\begin{bmatrix}` are exactly
    what a signals lecture writes. So an expression is cut into pieces around
    each environment: the plain pieces and every cell go through mathtext one
    at a time, and the cells are laid out on a grid — columns aligned the way
    the environment says, rows on a common baseline — between delimiters
    drawn as SVG paths stretched to the grid's height. The pieces are then
    stacked side by side on one baseline and the whole thing shipped as one
    SVG, the same shape a plain expression produces. Nested environments
    recurse. See _layout.

  * **Nothing here may fail the render.** matplotlib is optional and its
    parser still rejects some real LaTeX (`\\substack`, `\\overset`), so every
    failure degrades to cleaned-up text in a serif face. A summary with ugly
    maths still beats no PDF — the same rule the rest of the export follows.

  * **A plain expression's SVG is matplotlib's own file, untouched.** Only
    expressions holding an environment are re-assembled. Both report the same
    metrics, so the two paths line up on the page.
"""
import base64
import html as _html
import io
import os
import re

# The SVG's internal coordinate scale only. Point sizes are computed back out
# of it, so this number never reaches the page.
DPI = 100.0

# The body face is Computer Modern too (see pdf.DEFAULT_FONT_STACK), so maths
# set at the body size is the right size. A sans body with a taller x-height
# wants ~1.15 here — that is what the setting exists for.
DEFAULT_MATH_SCALE = 1.0

# Regions where a `$` is a dollar sign, not maths.
CODE_SPAN_RE = re.compile(r"```.*?```|~~~.*?~~~|`[^`\n]*`", re.DOTALL)

DISPLAY_RE = re.compile(r"\$\$(.+?)\$\$", re.DOTALL)
# Inline maths: no newline inside, no space just inside either delimiter (so
# "$5 and $6" doesn't match), and not adjacent to another '$'.
INLINE_RE = re.compile(r"(?<![\$\\])\$(?!\s)([^\$\n]+?)(?<![\s\\])\$(?!\$)")

TOKEN_PREFIX = "MTHX"
TOKEN_SUFFIX = "Z"
TOKEN_RE = re.compile(rf"{TOKEN_PREFIX}(\d+){TOKEN_SUFFIX}")

BEGIN_RE = re.compile(r"\\begin\{([A-Za-z]+\*?)\}")
ROW_SPLIT_RE = re.compile(r"\\\\(?:\s*\[[^\]]*\])?")

# Every environment the composer knows: (left delimiter, right delimiter,
# column alignment). Alignment is one letter for every column, or "rl" for
# the alternating right/left of aligned — `a &= b` puts the `=` flush against
# the `a`, which is what the ampersand is for. array reads its own spec.
ENVIRONMENTS = {
    "cases": ("{", "", "l"), "dcases": ("{", "", "l"), "rcases": ("", "}", "l"),
    "matrix": ("", "", "c"), "smallmatrix": ("", "", "c"),
    "pmatrix": ("(", ")", "c"), "bmatrix": ("[", "]", "c"),
    "Bmatrix": ("{", "}", "c"), "vmatrix": ("|", "|", "c"),
    "Vmatrix": ("\u2016", "\u2016", "c"),
    "aligned": ("", "", "rl"), "align": ("", "", "rl"),
    "align*": ("", "", "rl"), "split": ("", "", "rl"),
    "alignedat": ("", "", "rl"),
    "gather": ("", "", "c"), "gather*": ("", "", "c"), "gathered": ("", "", "c"),
    "array": (None, None, None),
}
# `\left( \begin{array}...\end{array} \right)` — the delimiter the author put
# around an environment, read as the environment's own.
_LEFT_RE = re.compile(r"\\left\s*(\\\{|\\\}|\\\||\\lbrace|\\rbrace|\\lvert|\\rvert|\\langle|\\rangle|[(\[{|.])\s*$")
_RIGHT_RE = re.compile(r"^\s*\\right\s*(\\\{|\\\}|\\\||\\lbrace|\\rbrace|\\lvert|\\rvert|\\langle|\\rangle|[)\]}|.])")
_DELIM_SPELLINGS = {
    "\\{": "{", "\\}": "}", "\\lbrace": "{", "\\rbrace": "}", "\\|": "\u2016",
    "\\lvert": "|", "\\rvert": "|", "\\langle": "<", "\\rangle": ">", ".": "",
}

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
    return bool(re.match(r"^\d", tex)) and bool(re.search(r"\s[A-Za-z]", tex))


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
    rows = _rows(tex) if display else [tex]
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


def _img_html(svg, width, height, depth, alt, display):
    cls = "math math-display" if display else "math math-inline"
    uri = "data:image/svg+xml;base64," + base64.b64encode(svg).decode("ascii")
    return (f'<img class="{cls}" src="{uri}" '
            f'alt="{_html.escape(alt, quote=True)}" '
            f'style="width:{width:.2f}pt;height:{height:.2f}pt;'
            f'vertical-align:{-depth:.2f}pt" />')


def _row_html(row, engine, size_pt, color, display):
    if engine is not None:
        if BEGIN_RE.search(row):
            # An environment: composed out of mathtext pieces, see _layout.
            try:
                box = _layout(row, engine, size_pt, color, display)
            except Exception:  # noqa: BLE001 - anything unparseable: fall back
                box = None
            if box is not None:
                return _img_html(_svg_document(box), box.width, box.height,
                                 box.depth, _flatten(row), display)
            return _fallback_html(row)
        result = _try_engine(row, engine, size_pt, color, display)
        if result is not None:
            svg, width, height, depth = result
            return _img_html(svg, width, height, depth, row, display)
    return _fallback_html(row)


def _try_engine(tex, engine, size_pt, color, display=False):
    """mathtext on the tidied form, then on the author's own; None if neither.

    The digit rewriting and the display-style fractions in _prepare are
    cosmetic, so they must never be what loses an expression that mathtext
    would otherwise have accepted; the last candidate is the author's
    spelling with every \\dfrac demoted, for a mathtext too old to know it.
    """
    plain = _compat(_flatten(tex))
    for candidate in (_prepare(tex, display), plain,
                      plain.replace("\\dfrac", "\\frac")):
        try:
            return engine(candidate, size_pt, color)
        except Exception:  # noqa: BLE001 - unparseable TeX, try the next
            continue
    return None


def _rows(tex):
    """Split display maths into rows on a top-level `\\\\`.

    A `\\\\` inside an environment is that environment's row break and is
    left to the grid; only the ones outside any \\begin...\\end stack the
    display into lines.
    """
    return [r for r in _split_top_level(tex)]


def _split_top_level(tex):
    out, depth, last, i = [], 0, 0, 0
    while i < len(tex):
        if tex.startswith("\\begin{", i):
            depth += 1
        elif tex.startswith("\\end{", i):
            depth -= 1
        elif depth == 0 and tex.startswith("\\\\", i):
            out.append(tex[last:i])
            m = ROW_SPLIT_RE.match(tex, i)
            i = m.end()
            last = i
            continue
        i += 1
    out.append(tex[last:])
    return out


def _flatten(tex):
    """One line, no alignment marks — mathtext has neither concept."""
    return re.sub(r"\s+", " ", tex.replace("&", " ")).strip()


# --------------------------------------------------------------------------
# Environments: cases, matrices, aligned — composed from mathtext pieces.
# --------------------------------------------------------------------------

class _Box:
    """An SVG fragment with its metrics, all in points.

    `body` draws in a local frame whose origin is the top-left corner and
    whose baseline sits at y = height - depth. `defs` are the glyph outlines
    it references, keyed by id, so a composite made of many mathtext pieces
    ships each glyph once.
    """
    __slots__ = ("width", "height", "depth", "body", "defs")

    def __init__(self, width, height, depth, body="", defs=None):
        self.width, self.height, self.depth = width, height, depth
        self.body, self.defs = body, defs or {}

    @property
    def ascent(self):
        return self.height - self.depth


def _find_env(tex, start=0):
    """The first environment at or after `start`: (begin, end, name, spec,
    body), with `end` just past the \\end — or None. Nesting of the same
    name is honoured so a matrix inside a matrix closes at the right place."""
    m = BEGIN_RE.search(tex, start)
    if not m:
        return None
    name = m.group(1)
    pos = m.end()
    spec = None
    if name == "array":
        s = re.match(r"\s*\{([^}]*)\}", tex[pos:])
        if s:
            spec = s.group(1)
            pos += s.end()
    depth = 1
    scan = pos
    begin_tag, end_tag = f"\\begin{{{name}}}", f"\\end{{{name}}}"
    while depth:
        nb = tex.find(begin_tag, scan)
        ne = tex.find(end_tag, scan)
        if ne == -1:
            raise ValueError(f"unterminated \\begin{{{name}}}")
        if nb != -1 and nb < ne:
            depth += 1
            scan = nb + len(begin_tag)
        else:
            depth -= 1
            scan = ne + len(end_tag)
    body = tex[pos:scan - len(end_tag)]
    return m.start(), scan, name, spec, body


def _segments(tex):
    """Cut an expression into ("tex", s) and ("env", name, spec, body, l, r).

    A `\\left X` just before an environment and the matching `\\right Y` just
    after it become that environment's delimiters — `\\left\\{ \\begin{array}
    ... \\right.` is how some authors spell cases — because mathtext's own
    \\left/\\right can't stretch around something it never sees.
    """
    out = []
    pos = 0
    while True:
        found = _find_env(tex, pos)
        if not found:
            break
        begin, end, name, spec, body = found
        if name not in ENVIRONMENTS:
            raise ValueError(f"unknown environment {name}")
        before = tex[pos:begin]
        after = tex[end:]
        left, right, _align = ENVIRONMENTS[name]
        lm, rm = _LEFT_RE.search(before), _RIGHT_RE.match(after)
        if lm and rm:
            left = _DELIM_SPELLINGS.get(lm.group(1), lm.group(1))
            right = _DELIM_SPELLINGS.get(rm.group(1), rm.group(1))
            before = before[:lm.start()]
            end += rm.end()
        if before.strip():
            out.append(("tex", before))
        out.append(("env", name, spec, body, left or "", right or ""))
        pos = end
    if tex[pos:].strip():
        out.append(("tex", tex[pos:]))
    return out


def _layout(tex, engine, size_pt, color, display=False):
    """Typeset an expression that may hold environments into one _Box."""
    boxes = []
    for seg in _segments(tex):
        if seg[0] == "tex":
            boxes.append(_text_box(seg[1], engine, size_pt, color, display))
        else:
            _kind, name, spec, body, left, right = seg
            boxes.append(_env_box(name, spec, body, left, right,
                                  engine, size_pt, color, display))
    if not boxes:
        raise ValueError("empty expression")
    return _hstack(boxes, gap=0.15 * size_pt)


def _text_box(tex, engine, size_pt, color, display=False):
    result = _try_engine(tex, engine, size_pt, color, display)
    if result is None:
        raise ValueError(f"mathtext rejected {tex!r}")
    svg, width, height, depth = result
    defs, body = _svg_parts(svg)
    return _Box(width, height, depth, body, defs)


_DEFS_RE = re.compile(r"<defs>(.*?)</defs>", re.DOTALL)
_DEF_PATH_RE = re.compile(r'<path id="([^"]+)"[^>]*/>', re.DOTALL)
_PATCH_RE = re.compile(r'<g id="patch_\d+">.*?</g>', re.DOTALL)


def _svg_parts(svg):
    """(defs, body) out of a matplotlib SVG: the glyph outlines by id, and
    the drawing that uses them with the figure/patch/text ids stripped, so
    several can share one document without colliding."""
    text = svg.decode("utf-8") if isinstance(svg, bytes) else svg
    defs = {}
    for block in _DEFS_RE.findall(text):
        for m in _DEF_PATH_RE.finditer(block):
            defs.setdefault(m.group(1), m.group(0))
    m = re.search(r'<g id="figure_1">(.*)</g>\s*</svg>', text, re.DOTALL)
    body = m.group(1) if m else ""
    body = _DEFS_RE.sub("", body)
    body = _PATCH_RE.sub("", body)
    body = re.sub(r"<!--.*?-->", "", body, flags=re.DOTALL)
    body = re.sub(r' id="(?:figure|text|axes|patch)_\d+"', "", body)
    return defs, body.strip()


# Environments whose cells LaTeX sets in text style even inside display
# maths: a fraction in a matrix entry stays small.
_TEXT_STYLE_ENVS = {"cases", "dcases", "rcases", "matrix", "smallmatrix",
                    "pmatrix", "bmatrix", "Bmatrix", "vmatrix", "Vmatrix",
                    "array"}


def _cells(body, engine, size_pt, color, display=False):
    """The environment body as rows of _Box cells."""
    rows = []
    for row in _split_top_level(body):
        row = re.sub(r"\\hline", "", row)
        if not row.strip():
            continue
        cells = []
        for cell in _split_top_level_amp(row):
            cell = cell.strip()
            if not cell:
                cells.append(_Box(0.0, 0.0, 0.0))
            elif BEGIN_RE.search(cell):
                cells.append(_layout(cell, engine, size_pt, color, display))
            else:
                cells.append(_text_box(cell, engine, size_pt, color, display))
        rows.append(cells)
    if not rows:
        raise ValueError("empty environment")
    return rows


def _split_top_level_amp(row):
    out, depth, last, i = [], 0, 0, 0
    while i < len(row):
        if row.startswith("\\begin{", i):
            depth += 1
        elif row.startswith("\\end{", i):
            depth -= 1
        elif row[i] == "\\":
            i += 2
            continue
        elif depth == 0 and row[i] == "&":
            out.append(row[last:i])
            last = i + 1
        i += 1
    out.append(row[last:])
    return out


def _alignments(name, spec, ncols):
    _l, _r, align = ENVIRONMENTS[name]
    if name == "array":
        letters = [c for c in (spec or "") if c in "clr"]
        return [(letters[i] if i < len(letters) else "c") for i in range(ncols)]
    if align == "rl":
        return ["r" if i % 2 == 0 else "l" for i in range(ncols)]
    return [align] * ncols


def _env_box(name, spec, body, left, right, engine, size_pt, color,
             display=False):
    rows = _cells(body, engine, size_pt, color,
                  display and name not in _TEXT_STYLE_ENVS)
    ncols = max(len(r) for r in rows)
    aligns = _alignments(name, spec, ncols)
    # cases separates value from condition by a \quad; a matrix by
    # \arraycolsep either side; aligned puts its columns nearly flush so the
    # relation lands beside its left-hand side.
    colgap = {"cases": 1.0, "dcases": 1.0, "rcases": 1.0}.get(name, 0.8)
    if ENVIRONMENTS[name][2] == "rl":
        colgap = 0.25
    grid = _grid(rows, aligns, colgap * size_pt, 0.3 * size_pt, size_pt)
    parts = []
    if left:
        parts.append(_delim_box(left, grid, size_pt, color, mirror=False))
    parts.append(grid)
    if right:
        parts.append(_delim_box(right, grid, size_pt, color, mirror=True))
    return _hstack(parts, gap=0.12 * size_pt if (left or right) else 0.0)


def _grid(rows, aligns, colgap, rowgap, size_pt):
    ncols = len(aligns)
    widths = [0.0] * ncols
    for row in rows:
        for c, box in enumerate(row):
            widths[c] = max(widths[c], box.width)
    ascents = [max(b.ascent for b in row) for row in rows]
    depths = [max(b.depth for b in row) for row in rows]
    total_w = sum(widths) + colgap * max(ncols - 1, 0)
    total_h = sum(a + d for a, d in zip(ascents, depths)) + rowgap * (len(rows) - 1)
    body, defs = [], {}
    y = 0.0
    for r, row in enumerate(rows):
        baseline = y + ascents[r]
        x = 0.0
        for c, box in enumerate(row):
            slack = widths[c] - box.width
            dx = {"l": 0.0, "r": slack, "c": slack / 2}[aligns[c]]
            if box.body:
                body.append(_placed(box, x + dx, baseline - box.ascent))
                defs.update(box.defs)
            x += widths[c] + colgap
        y += ascents[r] + depths[r] + rowgap
    # The grid is centred on the maths axis — a quarter em above the
    # baseline in Computer Modern — which is where the surrounding `=` sits.
    axis = 0.25 * size_pt
    depth = total_h / 2 - axis
    return _Box(total_w, total_h, depth, "".join(body), defs)


def _placed(box, x, y):
    return f'<g transform="translate({x:.3f} {y:.3f})">{box.body}</g>'


def _hstack(boxes, gap):
    ascent = max(b.ascent for b in boxes)
    depth = max(b.depth for b in boxes)
    body, defs = [], {}
    x = 0.0
    for i, box in enumerate(boxes):
        if i:
            x += gap
        if box.body:
            body.append(_placed(box, x, ascent - box.ascent))
            defs.update(box.defs)
        x += box.width
    return _Box(x, ascent + depth, depth, "".join(body), defs)


def _delim_box(kind, grid, size_pt, color, mirror):
    """A delimiter drawn as a stroked path, stretched to the grid's height.

    Computer Modern's extensible delimiters are assembled from glyph pieces;
    at 8pt a stroked curve of the same shape is indistinguishable from them
    and needs no glyph table. `mirror` flips a left shape into its right
    counterpart, so each shape is drawn once.
    """
    over = 0.1 * size_pt
    height = grid.height + 2 * over
    stroke = 0.06 * size_pt
    h = height - stroke
    w = {"(": 0.32, ")": 0.32, "[": 0.26, "]": 0.26, "{": 0.42, "}": 0.42,
         "|": 0.16, "\u2016": 0.3, "<": 0.3, ">": 0.3}.get(kind)
    if w is None:
        raise ValueError(f"unknown delimiter {kind!r}")
    w *= size_pt
    if kind in "()":
        d = f"M {w:.3f} 0 Q {-w:.3f} {h / 2:.3f} {w:.3f} {h:.3f}"
    elif kind in "[]":
        tick = 0.7 * w
        d = f"M {tick:.3f} 0 L 0 0 L 0 {h:.3f} L {tick:.3f} {h:.3f}"
    elif kind in "{}":
        r = min(0.28 * size_pt, h / 4)
        mid, xm = h / 2, w / 2
        d = (f"M {w:.3f} 0 Q {xm:.3f} 0 {xm:.3f} {r:.3f} "
             f"L {xm:.3f} {mid - r:.3f} Q {xm:.3f} {mid:.3f} 0 {mid:.3f} "
             f"Q {xm:.3f} {mid:.3f} {xm:.3f} {mid + r:.3f} "
             f"L {xm:.3f} {h - r:.3f} Q {xm:.3f} {h:.3f} {w:.3f} {h:.3f}")
    elif kind == "|":
        d = f"M {w / 2:.3f} 0 L {w / 2:.3f} {h:.3f}"
    elif kind == "\u2016":
        d = (f"M {w / 3:.3f} 0 L {w / 3:.3f} {h:.3f} "
             f"M {2 * w / 3:.3f} 0 L {2 * w / 3:.3f} {h:.3f}")
    else:  # angle brackets
        d = f"M {w:.3f} 0 L 0 {h / 2:.3f} L {w:.3f} {h:.3f}"
    flip = f"translate({w:.3f} 0) scale(-1 1) " if mirror else ""
    body = (f'<g transform="translate({stroke / 2:.3f} {stroke / 2:.3f})">'
            f'<path d="{d}" transform="{flip}" fill="none" '
            f'stroke="{color}" stroke-width="{stroke:.3f}" '
            f'stroke-linecap="round" stroke-linejoin="round"/></g>')
    return _Box(w + stroke, height, grid.depth + over, body)


def _svg_document(box):
    defs = "".join(box.defs.values())
    w, h = box.width, box.height
    return (f'<svg xmlns="http://www.w3.org/2000/svg" '
            f'xmlns:xlink="http://www.w3.org/1999/xlink" '
            f'width="{w:.3f}pt" height="{h:.3f}pt" viewBox="0 0 {w:.3f} {h:.3f}">'
            f'<defs><style type="text/css">*{{stroke-linejoin: round; '
            f'stroke-linecap: butt}}</style>{defs}</defs>'
            f'{box.body}</svg>').encode("utf-8")


# Spellings mathtext doesn't know, and the equivalent it does. Cheaper than
# losing the whole expression to the text fallback over one arrow.
_COMPAT = {
    "\\implies": "\\Rightarrow", "\\impliedby": "\\Leftarrow",
    "\\iff": "\\Leftrightarrow", "\\tfrac": "\\frac",
    "\\operatorname": "\\mathrm",
    "\\mbox": "\\mathrm", "\\textbf": "\\mathbf",
    "\\textit": "\\mathit", "\\lvert": "|", "\\rvert": "|",
    "\\nonumber": "", "\\notag": "", "\\limits": "",
    # The short relation names. mathtext knows only the long ones, and a
    # signals lecture writes "0 \\le t < T" in every other formula.
    "\\le": "\\leq", "\\ge": "\\geq", "\\ne": "\\neq",
    "\\lt": "<", "\\gt": ">",
}


def _compat(tex):
    """Rewrite LaTeX mathtext doesn't implement into what it does."""
    def _sub(match):
        return _COMPAT.get(match.group(0), match.group(0))
    tex = re.sub(r"\\[A-Za-z]+", _sub, tex)
    # mathtext puts nothing between an unlimited integral sign and what
    # follows, so "\int x" printed the x on top of the sign's tail.
    return re.sub(r"(\\o?int)(?![A-Za-z_^\\])\s*", r"\1\\, ", tex)


def _prepare(tex, display=False):
    tex = _compat(_flatten(tex))
    if display:
        # mathtext sets \frac in text style everywhere, so a display formula
        # came out with the small stacked fractions of running text. \dfrac
        # is its display-style fraction — full-size numerator and
        # denominator, the way $$...$$ prints in LaTeX.
        tex = re.sub(r"\\frac(?![A-Za-z])", r"\\dfrac", tex)
    return _upright_digits(tex)
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
