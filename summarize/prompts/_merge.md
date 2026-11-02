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
- Keep the section structure the partial summaries use, but where two parts
  cover the same topic (the same concept taught in two videos, a recap of an
  earlier part), fold them into one section.
- Consolidate, don't concatenate. The final document must be shorter than the
  partials together — aim for at most two thirds of their combined length —
  by cutting repetition, restatements, overlapping examples and filler, and by
  folding parallel material into tables. Never drop a fact that appears only
  once: a definition, a formula, a number, a decision, an action item.
- Keep the partials' style: terse bullets and tables, not paragraphs.
- {language_rule}

Partial summaries, in order:

{transcript}
