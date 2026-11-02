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
import unicodedata

ENV_VAR = "SUMMARY_LANGUAGE"
DEFAULT = "th"

# code -> (name in English, the rule sentence the prompt gets)
LANGUAGES = {
    "en": (
        "English",
        "Write the entire document in English even when the transcript is "
        "in another language, but keep proper nouns, product names, commands, "
        "code, mathematical notation, and on-screen identifiers verbatim. "
        "Every sentence is English: never leave words in the transcript's "
        "language, and never write a word in any other script (Chinese, "
        "Japanese, Arabic, Cyrillic …).",
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
        "transliterated English sentence structure. Never switch into English "
        "for the rest of a sentence — outside the parenthesised term and the "
        "verbatim items above, every sentence is Thai — and never write a word "
        "in any other script (Chinese, Japanese, Arabic, Cyrillic …).",
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


def output_language(env=None):
    """The configured code ('th' by default). Raises UnknownLanguage.
    `env` stands in for os.environ (the web UI passes .env as it is now)."""
    return normalize((os.environ if env is None else env).get(ENV_VAR))


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


# ---------------------------------------------------------------------------
# Script drift: a model writing words in a script nobody asked for
# ---------------------------------------------------------------------------
# The free OpenRouter model of 2026-10-16 (dots-3-note) wrote a Thai summary
# with 167 runs of Chinese, Arabic, Japanese and Cyrillic inside Thai words
# — ต้นทุนการซื้อ卖出, การ预测แนวโน้ม — which the PDF's fonts print as empty
# boxes. `foreign_runs` finds them so the backend can reject the answer.

# The script of every letter that is not Latin or Greek, from its Unicode
# name ("CJK UNIFIED IDEOGRAPH-5356" -> CJK). Letters of these first words
# are maths or decoration, never a language.
_NEUTRAL_SCRIPTS = {"LATIN", "GREEK", "MATHEMATICAL", "MODIFIER",
                    "FULLWIDTH", "DOUBLE-STRUCK", "SCRIPT", "BLACK-LETTER",
                    "SMALL", "PLANCK", "INFORMATION", "DIGAMMA"}
_SCRIPT_ALIASES = {"HIRAGANA": "JAPANESE", "KATAKANA": "JAPANESE",
                   "KATAKANA-HIRAGANA": "JAPANESE", "HANGUL": "KOREAN",
                   "CJK": "CJK"}
_TARGET_SCRIPTS = {"th": {"THAI"}, "en": set()}
# A script must have this many letters in the model's input to count as the
# input's own (a stray character in an ASR transcript does not).
SOURCE_SCRIPT_MIN = 20


def _script(ch):
    if ord(ch) < 0x250 or not unicodedata.category(ch).startswith("L"):
        return None
    try:
        word = unicodedata.name(ch).split()[0]
    except ValueError:
        return None
    if word in _NEUTRAL_SCRIPTS:
        return None
    return _SCRIPT_ALIASES.get(word, word)


def scripts_in(text, minimum=1):
    """{script: letter count} of every non-Latin script in `text` with at
    least `minimum` letters."""
    counts = {}
    for ch in text or "":
        script = _script(ch)
        if script:
            counts[script] = counts.get(script, 0) + 1
    return {k: v for k, v in counts.items() if v >= minimum}


def foreign_runs(answer, source="", code=None):
    """The runs of letters in `answer` whose script is neither the summary
    language's, nor present in `source` (the model's input: a Chinese
    lecture may be quoted in Chinese), nor Latin/Greek. In order of
    appearance; empty when the answer is clean."""
    allowed = set(_TARGET_SCRIPTS.get(code or output_language(), set()))
    allowed |= set(scripts_in(source, SOURCE_SCRIPT_MIN))
    runs, current, current_script = [], [], None
    for ch in answer or "":
        script = _script(ch)
        if script and script not in allowed:
            if current and script != current_script:
                runs.append("".join(current))
                current = []
            current.append(ch)
            current_script = script
        elif current and unicodedata.category(ch).startswith("M"):
            current.append(ch)  # a combining mark belongs to its letter
        elif current:
            runs.append("".join(current))
            current, current_script = [], None
    if current:
        runs.append("".join(current))
    return runs
