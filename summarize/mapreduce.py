#!/usr/bin/env python3
"""
Map-reduce summarization for long meetings.

Map: each chunk (its transcript slice plus the frames that were on screen
during it) is summarized concurrently. Reduce: one final call merges the
partials into a single document under the same structure the prompt asks for.

The merge call is what keeps this from reading like stapled-together notes —
action items scattered across three chunks get collected into one list, and
repetition across the overlap regions gets folded together.

Concurrency is bounded by SUMMARY_MAX_PARALLEL (default 3). It's deliberately
small: every chunk carries images, and firing a dozen multi-megabyte requests
at a provider is a good way to earn the 429s that retry.py then has to sit out.
"""
import hashlib
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from chunking import max_parallel  # noqa: E402
import language  # noqa: E402

PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"
MERGE_PROMPT_PATH = PROMPTS_DIR / "_merge.md"

# Appended to the operator's chosen prompt for each chunk — appended, not
# prepended. The part label is the one thing that differs between the chunks
# of a run, and prompt caching keys on a byte-identical prefix: with the label
# at the front, the reference material behind it (identical for every chunk,
# often 10k+ tokens) never cached. At the end it sits after everything that
# varies anyway. No braces in here: the combined template goes through
# .format() later, and a stray brace would be read as a placeholder.
CHUNK_PREAMBLE = (
    "You are summarizing ONE PART of a longer recording, or of several "
    "recordings being summarized together — {part_label}. "
    "Cover this part thoroughly and do not speculate about what happened in the "
    "other parts. Your output will later be merged with the summaries of the "
    "other parts, so keep speaker attributions intact, and when the part label "
    "names a video, say which video the material comes from. The part label "
    "is for orientation only: do not copy its times into the summary.\n\n"
)


def _default_merge_template():
    """Fallback merge instructions if prompts/_merge.md is missing.

    Kept in code as well as on disk so an incomplete checkout still merges
    rather than failing the whole stage at the very last step.
    """
    return (
        "You are merging several partial summaries of the SAME meeting, given "
        "in chronological order, into one final document.\n\n"
        "Rules:\n"
        "- Produce ONE coherent document, not a list of parts. Never mention "
        "that it was assembled from parts.\n"
        "- If the partials open with a `# Title` heading, keep exactly one, "
        "as the first line.\n"
        "- Merge duplicates: the parts overlap slightly, so the same point may "
        "appear more than once.\n"
        "- Collect all action items into a single list with owners and due "
        "dates where stated.\n"
        "- Keep callout boxes (`> [!TAG] Title`), LaTeX and code blocks "
        "exactly as written.\n"
        "- Keep the section structure used by the partial summaries.\n"
        "- {language_rule}\n\n"
        "Partial summaries:\n\n{transcript}\n"
    )


def merge_prompt_path(prompt_name=None):
    """The merge instructions for a prompt: `_merge-<name>.md` when the
    prompt has its own (reality: keep the timestamps and quotes, results
    last), else the shared `_merge.md`."""
    if prompt_name:
        own = PROMPTS_DIR / f"_merge-{Path(prompt_name).stem}.md"
        if own.is_file():
            return own
    return MERGE_PROMPT_PATH


def load_merge_template(prompt_name=None):
    try:
        text = merge_prompt_path(prompt_name).read_text()
    except OSError:
        text = _default_merge_template()
    if "# Input" in text:
        text = text.split("# Input", 1)[1]
    # The merge writes the final document, so it is told the output language
    # outright rather than left to infer it from partials that already obey
    # the same rule — one setting, one answer, no drift on the last call.
    return language.apply(text).strip() + "\n\n"


# Finished chunk summaries are kept in the run directory, so a resume (a
# failed merge, a chunk that failed, a re-merge after the merge prompt
# changed) does not pay for the chunks that already succeeded. Keyed on
# everything that shapes the answer: the full chunk prompt, the transcript
# slice, the frames offered and the configured backends/models. Change any
# of them and the chunk is summarized again. Only inside a pipeline run
# (MEETING_BOT_RUN_DIR); a standalone summarize.py keeps nothing.
_CACHE_ENV = ("SUMMARY_BACKEND", "SUMMARY_FALLBACK_CHAIN", "OPENROUTER_MODEL",
              "CLAUDE_CLI_MODEL", "GEMINI_MODEL", "SUMMARY_LANGUAGE",
              "SUMMARY_EFFORT")


def _chunk_cache_dir():
    if os.environ.get("SUMMARY_CHUNK_CACHE", "1").strip().lower() in (
            "0", "false", "no", "off"):
        return None
    run_dir = os.environ.get("MEETING_BOT_RUN_DIR")
    if not run_dir or not Path(run_dir).is_dir():
        return None
    return Path(run_dir) / "chunks"


def _chunk_key(template, chunk):
    h = hashlib.sha256()
    for piece in (template, chunk.text,
                  *(f"{getattr(f, 'number', '')}:{f.path}" for f in chunk.frames),
                  *(f"{k}={os.environ.get(k, '')}" for k in _CACHE_ENV)):
        h.update(piece.encode("utf-8", "replace"))
        h.update(b"\0")
    return h.hexdigest()[:24]


def _cache_read(cache_dir, key):
    try:
        data = json.loads((cache_dir / f"{key}.json").read_text())
        return data["text"], data.get("models") or []
    except (OSError, ValueError, KeyError, TypeError):
        return None, []


def _cache_write(cache_dir, key, text, models):
    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
        tmp = cache_dir / f".{key}.tmp"
        tmp.write_text(json.dumps({"text": text, "models": models},
                                  ensure_ascii=False))
        tmp.replace(cache_dir / f"{key}.json")
    except OSError:
        pass   # a lost cache costs one call, never a run


def summarize_chunked(chunks, prompt_template, summarize_fn, log=print,
                      merge_template=None):
    """Summarize chunks in parallel, then merge.

    `summarize_fn(frames, transcript, template, role=None) -> str` is llm_client.summarize,
    so each chunk and the merge all inherit the configured backend, its retry
    policy, and the fallback chain. `merge_template` defaults to the shared
    `_merge.md`; summarize.py passes load_merge_template(<prompt>).
    """
    total = len(chunks)
    log(f"==> Long transcript: summarizing {total} chunks "
        f"({max_parallel()} at a time), then merging")

    chunk_template = prompt_template.rstrip() + "\n\n" + CHUNK_PREAMBLE.replace(
        "{part_label}", "PART_LABEL_PLACEHOLDER"
    ).rstrip() + "\n"

    results = [None] * total
    errors = []

    cache_dir = _chunk_cache_dir()
    import llm_client   # the used-models record; imported late (no cycle)

    def _one(chunk):
        template = chunk_template.replace("PART_LABEL_PLACEHOLDER",
                                          chunk.header(total))
        key = _chunk_key(template, chunk) if cache_dir else None
        if key:
            text, models = _cache_read(cache_dir, key)
            if text:
                log(f"    chunk {chunk.index + 1}/{total}: reusing the summary "
                    f"from an earlier attempt ({', '.join(models) or '?'})")
                for label in models:
                    backend, _, model = label.partition("/")
                    llm_client._record_used(backend, model)
                return text
        text = summarize_fn(chunk.frames, chunk.text, template)
        if key and text:
            last = llm_client.thread_last_used()
            _cache_write(cache_dir, key, text, [last] if last else [])
        return text

    with ThreadPoolExecutor(max_workers=max_parallel()) as pool:
        futures = {pool.submit(_one, c): c for c in chunks}
        for future in as_completed(futures):
            chunk = futures[future]
            try:
                results[chunk.index] = future.result()
                log(f"    chunk {chunk.index + 1}/{total} done "
                    f"({len(results[chunk.index])} chars)")
            except Exception as exc:  # noqa: BLE001
                # One chunk failing shouldn't throw away the other N-1, which
                # each cost a real API call. Merge what we have and say so.
                log(f"    chunk {chunk.index + 1}/{total} FAILED: "
                    f"{type(exc).__name__}: {exc}")
                errors.append((chunk.index, exc))

    # A chunk that stopped because the subscription window ran out (and the
    # in-process wait gave up) is not a chunk to merge around: the document
    # would ship with a hole in it, and the stage would be marked done, so
    # nothing would ever fill it. Fail the stage instead — the reset time
    # travels with the exception, run_one.sh records it, and --resume-all
    # comes back for it once the window has reset. Duck-typed on `pause_run`
    # so this module stays independent of llm_client's exception classes.
    paused = [exc for _, exc in errors if getattr(exc, "pause_run", False)]
    if paused:
        log(f"==> {len(paused)} of {total} chunks are waiting on the Claude "
            f"usage window; the stage pauses rather than merging without them")
        raise paused[0]

    done = [r for r in results if r]
    if not done:
        raise RuntimeError(
            f"Every chunk failed ({len(errors)} of {total}); "
            f"first error: {errors[0][1] if errors else 'unknown'}"
        )

    if errors:
        log(f"==> WARNING: {len(errors)} of {total} chunks failed; merging the "
            f"{len(done)} that succeeded. Re-run to fill the gaps.")

    parts = []
    for i, text in enumerate(results):
        if not text:
            parts.append(f"## {chunks[i].header(total)}\n\n"
                         f"*(this part could not be summarized)*")
            continue
        parts.append(f"## {chunks[i].header(total)}\n\n{text}")
    combined = "\n\n---\n\n".join(parts)

    log(f"==> Merging {len(done)} partial summaries into the final document")
    # role="merge" lets the claude-cli backend put this call on a cheaper
    # model: it folds partials a stronger model already wrote, and its output
    # is about the size of everything it read.
    merged = summarize_fn([], combined, merge_template or load_merge_template(),
                          role="merge")

    if errors:
        missing = ", ".join(str(i + 1) for i, _ in sorted(errors))
        merged += (
            f"\n\n---\n\n> **Incomplete:** part(s) {missing} of {total} could "
            f"not be summarized and are missing from this document. "
            f"Re-run the summarize stage to fill them in.\n"
        )
    return merged
