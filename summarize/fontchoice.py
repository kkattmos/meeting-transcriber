#!/usr/bin/env python3
"""The PDF's body face: which fonts a run may choose, and how big each is set.

Settled with the operator 2026-09-29. A run picks its output language (th or
en, SUMMARY_LANGUAGE) and one body font from a short list per language:

    th: Bai Jamjuree, Sarabun                 (Computer Modern has no Thai)
    en: CMU Serif (Computer Modern), Sarabun, Bai Jamjuree

The default for each language comes from PDF_FONT_TH / PDF_FONT_EN in .env
(Bai Jamjuree and CMU Serif when unset); a run can override it with
`pipeline.sh --pdf-font`, which run_one.sh exports to summarize.py as
PDF_FONT. The choice is recorded in the document's provenance, so a later
re-render keeps it whatever the box is set to by then.

**The three are set to look the same size, not to the same point size.** A
nominal 9.5pt means "Computer Modern at 9.5pt"; the other faces are scaled
so their lower-case letters are as tall as CM's (x-height matching). The
x-heights below were measured from the font files (OS/2 sxHeight, and the
bounds of the `x` glyph where the table has none):

    CMU Serif 0.431 em   Sarabun 0.500 em   Bai Jamjuree 0.499 em

so Sarabun at a nominal 9.5pt is set at 8.19pt. Maths is always Computer
Modern at the nominal size, which is what keeps it level with the text
whichever face the text is in.

Also the command-line check pipeline.sh runs before anything is paid for:

    python3 summarize/fontchoice.py check --language th --font sarabun
    python3 summarize/fontchoice.py options          (JSON, for the web UI)
"""
import json
import os
import sys

CMU = "CMU Serif"
SARABUN = "Sarabun"
BAI = "Bai Jamjuree"

CHOICES = {
    "th": (BAI, SARABUN),
    "en": (CMU, SARABUN, BAI),
}
BUILTIN_DEFAULTS = {"th": BAI, "en": CMU}
DEFAULT_ENV = {"th": "PDF_FONT_TH", "en": "PDF_FONT_EN"}
# The per-run choice, exported by run_one.sh from state.json.
RUN_ENV = "PDF_FONT"

# x-height / em, measured from the files — see the module docstring.
X_HEIGHT = {CMU: 0.431, SARABUN: 0.500, BAI: 0.499}
REFERENCE_X_HEIGHT = X_HEIGHT[CMU]
# JetBrains Mono, for code: set to the same x-height as the text too.
MONO_X_HEIGHT = 0.550

# What each face falls back to for the glyphs it lacks. Computer Modern has
# no Thai, so an English sheet in CMU sets a Thai name in Sarabun; the Thai
# faces carry Latin themselves.
FALLBACKS = {
    CMU: (SARABUN, "Noto Serif Thai", "Latin Modern Roman", "DejaVu Serif",
          "serif"),
    SARABUN: (BAI, "Noto Sans Thai", "DejaVu Sans", "sans-serif"),
    BAI: (SARABUN, "Noto Sans Thai", "DejaVu Sans", "sans-serif"),
}
MATH_STACK = ("CMU Serif", "Latin Modern Math", "Latin Modern Roman",
              "STIX Two Math", "DejaVu Serif", "serif")
MONO_STACK = ("JetBrains Mono", "JetBrainsMono Nerd Font", "JetBrainsMono NF",
              "DejaVu Sans Mono", "monospace")

_ALIASES = {
    "cmu": CMU, "cmu serif": CMU, "cm": CMU, "computer modern": CMU,
    "computermodern": CMU, "latin modern": CMU, "cmu-serif": CMU,
    "sarabun": SARABUN, "th sarabun": SARABUN, "thsarabun": SARABUN,
    "bai jamjuree": BAI, "baijamjuree": BAI, "bai-jamjuree": BAI, "bai": BAI,
}


class UnknownFont(ValueError):
    """A font name that is not on the list for the language."""


def _lang(lang):
    import language
    return language.normalize(lang)


def normalize(value, lang):
    """Fold a font name to its canonical spelling, or raise UnknownFont."""
    code = _lang(lang)
    key = " ".join((value or "").strip().lower().split())
    font = _ALIASES.get(key)
    if font is None or font not in CHOICES[code]:
        raise UnknownFont(
            f"font {value!r} is not offered for {code}; choose one of "
            f"{', '.join(CHOICES[code])}")
    return font


def default_font(lang):
    """PDF_FONT_<LANG> when it names a font on the list, else the built-in."""
    code = _lang(lang)
    raw = os.environ.get(DEFAULT_ENV[code])
    if raw and raw.strip():
        try:
            return normalize(raw, code)
        except UnknownFont as exc:
            print(f"  warning: {DEFAULT_ENV[code]}: {exc}; using "
                  f"{BUILTIN_DEFAULTS[code]}", file=sys.stderr)
    return BUILTIN_DEFAULTS[code]


def chosen_font(lang, value=None):
    """The font a render uses: `value` (the document's own record), else the
    run's PDF_FONT, else the language default. A choice that is not valid for
    the language is reported and replaced by the default, never fatal — the
    PDF is not allowed to take a run down."""
    code = _lang(lang)
    for candidate in (value, os.environ.get(RUN_ENV)):
        if candidate and candidate.strip():
            try:
                return normalize(candidate, code)
            except UnknownFont as exc:
                print(f"  warning: {exc}; using the default", file=sys.stderr)
    return default_font(code)


def size_factor(font):
    """How much smaller (or larger) than nominal `font` is set, so its
    x-height matches Computer Modern's at the nominal size."""
    return REFERENCE_X_HEIGHT / X_HEIGHT.get(font, REFERENCE_X_HEIGHT)


def mono_factor():
    return REFERENCE_X_HEIGHT / MONO_X_HEIGHT


def css_stack(names):
    return ", ".join(f'"{n}"' if " " in n and not n.startswith('"') else n
                     for n in names)


def body_stack(font):
    return css_stack((font,) + FALLBACKS.get(font, ("serif",)))


def options():
    """What the web UI offers: the choices and the defaults per language."""
    import language
    return {
        "languages": sorted(CHOICES),
        "default_language": language.output_language(),
        "fonts": {code: list(names) for code, names in CHOICES.items()},
        "default_fonts": {code: default_font(code) for code in CHOICES},
    }


def _main(argv):
    import argparse
    ap = argparse.ArgumentParser(description="PDF font choice")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("check", help="print the canonical font, or fail")
    p.add_argument("--language", required=True)
    p.add_argument("--font", required=True)
    sub.add_parser("options", help="the choices and defaults, as JSON")
    args = ap.parse_args(argv[1:])
    import language
    try:
        if args.cmd == "check":
            print(normalize(args.font, args.language))
        else:
            print(json.dumps(options()))
    except (UnknownFont, language.UnknownLanguage) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    raise SystemExit(_main(sys.argv))
