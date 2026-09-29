"""The prompt names: the four that exist, and what the old names became.

Stdlib only, so the web UI (trigger_server.py) can show the right default
without importing the summarizer. summarize.py resolves --prompt through
canonical_prompt_name() too, so the two cannot disagree.

Four prompts since 2026-09-29 — video, meeting, lecture, tutorial — each one
file for every backend. `video` is the default when neither --prompt nor
SUMMARY_PROMPT names one.
"""

DEFAULT_PROMPT = "video"

# The names the prompts had before they were merged. An .env, a state.json or
# a phone shortcut written before then still says one of these, and must keep
# resolving to the prompt that replaced it rather than failing a resume.
PROMPT_ALIASES = {
    "lecture-claude": "lecture", "lecture-gemini": "lecture",
    "lecture-gemini-old": "lecture", "lecture-reference": "lecture",
    "tutorial-claude": "tutorial", "tutorial-gemini": "tutorial",
    "tutorial-gemini-old": "tutorial",
    "meeting-claude": "meeting", "meeting-gemini": "meeting",
    "meeting-gemini-old": "meeting",
    # Both were meeting summarizers.
    "summarize": "meeting", "summarize-v2": "meeting",
}


def canonical_prompt_name(prompt_name):
    """The prompt a --prompt/SUMMARY_PROMPT value stands for, without .md:
    the default for None, and the replacement for a pre-merge name."""
    if not prompt_name:
        return DEFAULT_PROMPT
    stem = prompt_name[:-3] if prompt_name.endswith(".md") else prompt_name
    return PROMPT_ALIASES.get(stem.lower(), stem)
