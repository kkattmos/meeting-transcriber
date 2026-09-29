# Merge partial summaries

Used only by the map-reduce path, when a transcript was too long to summarize in
one call (see `summarize/mapreduce.py`). The leading underscore keeps it out of
the `--prompt` menu — it is never a valid choice for `--prompt`, because it
expects partial summaries as input rather than a transcript.

# Input

You are merging several partial summaries of the SAME recording into one final
document. They are given in chronological order and were produced independently,
so they overlap slightly at the boundaries.

Rules:

- Produce ONE coherent document. Never mention that it was assembled from parts,
  and do not keep the "part N of M" headings.
- Keep exactly one top-level `# Title` heading, as the first line of the
  document. Never repeat it between sections.
- Renumber the `## N.` sections sequentially from 1 across the whole document.
- Merge duplicates. Because the parts overlap, the same decision, topic, formula
  or callout box may appear in two consecutive summaries — state it once.
- Keep every callout box (`> [!CONCEPT] …`, `> [!EXAMPLE] …`, `> [!WARNING] …`,
  `> [!IMPORTANT] …`, `> [!NOTE] …`) exactly in that form, with every line of
  the box starting with `>`. If the partials each open with an `[!IMPORTANT]`
  box, combine them into one near the top.
- Collect every action item into a single list or table, preserving owners and
  due dates wherever they were given.
- Keep LaTeX (`$...$`, `$$...$$`) and fenced code blocks byte for byte.
- Do not add timestamps, time ranges or frame references; the partials have none.
- Keep the section structure the partial summaries use.
- Preserve the level of detail. This is a merge, not a further summarization —
  do not compress the partials into something shorter than they collectively are.
- {language_rule}

Partial summaries, in order:

{transcript}
