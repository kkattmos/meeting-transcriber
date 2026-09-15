"""The language the summary is written in (SUMMARY_LANGUAGE).

One setting, read in one place, that decides what the prompts tell the model
to write in. The transcript's language is a separate matter — that is
ASSEMBLYAI_LANGUAGE, and it says what the *audio* is in; this says what the
*notes* come out in. A Thai lecture summarized in English and an English
lecture summarized in Thai are both valid pairs.

Every prompt template carries a `{language_rule}` placeholder inside its
numbered rules; `apply()` fills it with the sentence for the configured
language before the template goes anywhere near a model. The placeholder
sits in the prompt's static (cacheable) half on purpose: the setting is
per-box, not per-run, so the content-addressed system prompt file changes
once when the operator flips it and then stays put.

The wrapper the code builds around the body (`Youtube Link:`, `View
Transcript`, the PDF's appendix headings) stays in English whatever this
says — see document.py; the .md has to drop into the operator's existing
course files.
"""

import os

ENV_VAR = "SUMMARY_LANGUAGE"
DEFAULT = "th"

# code -> (name in English, the rule sentence the prompt gets)
LANGUAGES = {
    "en": (
        "English",
        "Write the entire document in English even when the transcript is "
        "in another language, but keep proper nouns, product names, commands, "
        "code, mathematical notation, and on-screen identifiers verbatim.",
    ),
    "th": (
        "Thai",
        "Write the entire document — headings, prose, tables, list items — in "
        "Thai (ภาษาไทย) even when the transcript is in another language. On "
        "the first use of each technical term write the Thai term followed by "
        "the English term in parentheses, e.g. การแปลงฟูเรียร์ (Fourier "
        "transform), and use the Thai term alone afterwards; where no "
        "established Thai term exists, use the English term as-is. Keep "
        "proper nouns, product names, commands, code, mathematical notation, "
        "and on-screen identifiers verbatim — never translate LaTeX, code, "
        "or variable names. Use ordinary Thai academic register, not "
        "transliterated English sentence structure.",
    ),
}

# Spellings an operator might reasonably put in .env, folded to the code.
_ALIASES = {
    "thai": "th", "th-th": "th", "th_th": "th", "ไทย": "th", "ภาษาไทย": "th",
    "english": "en", "en-us": "en", "en_us": "en", "en-gb": "en",
}

PLACEHOLDER = "{language_rule}"


class UnknownLanguage(ValueError):
    """SUMMARY_LANGUAGE names a language there is no rule for."""


def normalize(value):
    """Fold a SUMMARY_LANGUAGE value to a code in LANGUAGES, or raise."""
    code = (value or "").strip().lower()
    if not code:
        return DEFAULT
    code = _ALIASES.get(code, code)
    if code not in LANGUAGES:
        raise UnknownLanguage(
            f"{ENV_VAR}={value!r} is not supported; use one of "
            f"{', '.join(sorted(LANGUAGES))}")
    return code


def output_language():
    """The configured code ('th' by default). Raises UnknownLanguage."""
    return normalize(os.environ.get(ENV_VAR))


def language_name(code=None):
    return LANGUAGES[code or output_language()][0]


def language_rule(code=None):
    return LANGUAGES[code or output_language()][1]


def apply(template, code=None):
    """Fill the template's {language_rule} placeholder for the configured
    language. A template without the placeholder is returned unchanged, so a
    custom prompt file that hard-codes its own language keeps working."""
    if PLACEHOLDER not in template:
        return template
    return template.replace(PLACEHOLDER, language_rule(code))
