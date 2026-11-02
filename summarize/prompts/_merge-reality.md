# Merge partial recaps of one reality-show episode

Used only by the map-reduce path, for the `reality` prompt, when an episode's
transcript was too long to recap in one call (see `summarize/mapreduce.py`,
`load_merge_template`). The leading underscore keeps it out of the `--prompt`
menu. Unlike `_merge.md`, the partials carry timestamps and quotes, and the
results belong at the end.

# Input

You are merging several partial recaps of the SAME reality-show episode into
one final recap. They are given in chronological order and were produced
independently, so they overlap slightly at the boundaries.

Rules:

- Produce ONE coherent recap. Never mention that it was assembled from parts,
  and do not keep the "part N of M" headings.
- Keep exactly one top-level `# Title` heading, as the first line.
- Keep one opening paragraph and one "Teams" box near the top (take them from
  the first partial; add teams or contestants a later partial names). The
  opening reveals no result.
- The numbered `## N.` sections follow the episode in order; renumber them
  sequentially from 1. Where two partials describe the same moment (the
  overlap), state it once.
- Keep every timestamp exactly as written — `[mm:ss]`, `[h:mm:ss]` or
  `[Video N, mm:ss]`, in square brackets — and never add, remove, round or
  reformat one. Keep section headings' timestamps too.
- Keep every quote word for word, with its speaker, role, team and timestamp,
  and every callout box (`> [!EXAMPLE] Quotes`, `> [!IMPORTANT] …`,
  `> [!NOTE] …`) in that form, every line starting with `>`. If two partials
  name the same speaker differently, prefer the spelling taken from on-screen
  captions, and use it throughout.
- Build ONE `## Highlights` table from the partials' highlight rows: the five
  to ten strongest moments of the whole episode, in episode order.
- Build ONE `## Results` section, LAST, from every partial's results: the
  winner, prize, nominees, who was eliminated, who was saved, and who decided.
  A partial that reports "not shown" is overridden by one that shows it. If
  no partial shows a result, say so plainly.
- Preserve the level of detail. This is a merge, not a further summarization.
- {language_rule}

Partial recaps, in order:

{transcript}
