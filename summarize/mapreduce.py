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
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from chunking import max_parallel  # noqa: E402

PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"
MERGE_PROMPT_PATH = PROMPTS_DIR / "_merge.md"

# Prepended to the operator's chosen prompt for each chunk. No braces in here:
# the combined template goes through .format() later, and a stray brace would
# be read as a placeholder.
CHUNK_PREAMBLE = (
    "You are summarizing ONE PART of a longer meeting recording — {part_label}. "
    "Cover this part thoroughly and do not speculate about what happened in the "
    "other parts. Your output will later be merged with the summaries of the "
    "other parts, so keep timestamps and speaker attributions intact.\n\n"
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
        "- Merge duplicates: the parts overlap slightly, so the same point may "
        "appear more than once.\n"
        "- Collect all action items into a single list with owners and due "
        "dates where stated.\n"
        "- Preserve timestamp citations exactly as given.\n"
        "- Keep the section structure used by the partial summaries.\n"
        "- Write in the same language as the source material.\n\n"
        "Partial summaries:\n\n{transcript}\n"
    )


def load_merge_template():
    try:
        text = MERGE_PROMPT_PATH.read_text()
    except OSError:
        return _default_merge_template()
    if "# Input" in text:
        text = text.split("# Input", 1)[1]
    return text.strip() + "\n\n"


def summarize_chunked(chunks, prompt_template, summarize_fn, log=print):
    """Summarize chunks in parallel, then merge.

    `summarize_fn(frames, transcript, template) -> str` is llm_client.summarize,
    so each chunk and the merge all inherit the configured backend, its retry
    policy, and the fallback chain.
    """
    total = len(chunks)
    log(f"==> Long transcript: summarizing {total} chunks "
        f"({max_parallel()} at a time), then merging")

    chunk_template = CHUNK_PREAMBLE.replace(
        "{part_label}", "PART_LABEL_PLACEHOLDER"
    ) + prompt_template

    results = [None] * total
    errors = []

    def _one(chunk):
        template = chunk_template.replace("PART_LABEL_PLACEHOLDER",
                                          chunk.header(total))
        return summarize_fn(chunk.frames, chunk.text, template)

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
    merged = summarize_fn([], combined, load_merge_template())

    if errors:
        missing = ", ".join(str(i + 1) for i, _ in sorted(errors))
        merged += (
            f"\n\n---\n\n> **Incomplete:** part(s) {missing} of {total} could "
            f"not be summarized and are missing from this document. "
            f"Re-run the summarize stage to fill them in.\n"
        )
    return merged
