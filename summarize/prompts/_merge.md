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
- Merge duplicates. Because the parts overlap, the same decision or topic may
  appear in two consecutive summaries — state it once.
- Collect every action item into a single list, preserving owners and due dates
  wherever they were given.
- Preserve timestamp citations exactly as they appear. Do not renumber or
  recompute them.
- Keep the section structure the partial summaries use (key decisions, action
  items, topics discussed, slides/visuals referenced, or whatever the source
  prompt asked for).
- Preserve the level of detail. This is a merge, not a further summarization —
  do not compress the partials into something shorter than they collectively are.
- Write in the same language as the partial summaries.

Partial summaries, in order:

{transcript}
