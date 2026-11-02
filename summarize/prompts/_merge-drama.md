# Merge partial recaps of one Thai drama episode

Used only by the map-reduce path, for the `drama` prompt, when an episode's
transcript was too long to recap in one call (see `summarize/mapreduce.py`,
`load_merge_template`). The leading underscore keeps it out of the `--prompt`
menu. Like `_merge-reality.md`, the partials carry timestamps and quotes,
and the turning points belong at the end.

# Input

You are merging several partial recaps of the SAME Thai drama episode into
one final recap. They are given in chronological order and were produced
independently, so they overlap slightly at the boundaries.

Rules:

- Produce ONE coherent recap. Never mention that it was assembled from parts,
  and do not keep the "part N of M" headings.
- Keep exactly one top-level `# Title` heading, as the first line.
- Keep one spoiler-free opening paragraph and one "Characters" box near the
  top (take them from the first partial; add characters a later partial
  introduces). Neither reveals anything the episode reveals later.
- The numbered `## N.` sections follow the episode in order; renumber them
  sequentially from 1. Where two partials describe the same scene (the
  overlap), state it once.
- Keep every timestamp exactly as written — `[mm:ss]`, `[h:mm:ss]` or
  `[Video N, mm:ss]`, in square brackets — and never add, remove, round or
  reformat one. Keep section headings' timestamps too.
- Keep every quote word for word, with its speaker, addressee and timestamp,
  and every callout box (`> [!EXAMPLE] Quotes`, `> [!IMPORTANT] …`,
  `> [!NOTE] …`) in that form, every line starting with `>`. If two partials
  name the same character differently, prefer the spelling taken from
  on-screen text, and use it throughout.
- Build ONE `## Highlights` table from the partials' highlight rows: the five
  to ten strongest moments of the whole episode, in episode order.
- Build ONE `## What changed` section from every partial's: what is different
  by the END of the episode (a later partial overrides an earlier one), and
  the cliffhanger from the last partial. Then the `## Next episode` preview,
  if a partial has one, LAST.
- Preserve the level of detail. This is a merge, not a further summarization.
- {language_rule}

Partial recaps, in order:

{transcript}
