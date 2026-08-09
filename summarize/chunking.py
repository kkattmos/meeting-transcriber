#!/usr/bin/env python3
"""
Split a long meeting into chunks that can be summarized in parallel, then merge
the partials back into one document (map-reduce).

Why: a three-hour lecture is one enormous request. It's slow, it risks the
model's context limit, and detail from the middle gets flattened. Summarizing
slices concurrently is faster in wall-clock terms and keeps more of each
section, at the cost of one extra merge call to restore coherence.

Chunking uses the .srt when one exists next to the .txt, because that's the only
place segment timestamps live — and timestamps are what let each chunk carry the
frames that were on screen while those words were spoken. Without an .srt we
fall back to splitting the plain text and dividing the frames proportionally,
which is approximate but still better than sending every frame to every chunk.

Env vars:
  SUMMARY_CHUNK_CHARS      default 24000  (0 disables chunking entirely)
  SUMMARY_CHUNK_OVERLAP    default 800    (chars of context repeated between
                                           chunks, so a sentence split across a
                                           boundary isn't lost)
  SUMMARY_MAX_PARALLEL     default 3      (concurrent chunk requests)
"""
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

SRT_TIME = re.compile(
    r"(\d{2}):(\d{2}):(\d{2})[,.](\d{3})\s*-->\s*(\d{2}):(\d{2}):(\d{2})[,.](\d{3})"
)


@dataclass
class Segment:
    """One timed line of transcript."""
    start_s: float
    end_s: float
    text: str


@dataclass
class Chunk:
    """A slice of the meeting: its text, its time window, and its frames."""
    index: int
    text: str
    start_s: Optional[float] = None
    end_s: Optional[float] = None
    frames: List = field(default_factory=list)

    def header(self, total):
        if self.start_s is None:
            return f"part {self.index + 1} of {total}"
        return (f"part {self.index + 1} of {total} "
                f"({_hhmmss(self.start_s)}–{_hhmmss(self.end_s)})")


def _hhmmss(seconds):
    seconds = int(seconds or 0)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def chunk_chars():
    try:
        return int(os.environ.get("SUMMARY_CHUNK_CHARS", 24000))
    except ValueError:
        return 24000


def chunk_overlap():
    try:
        return max(0, int(os.environ.get("SUMMARY_CHUNK_OVERLAP", 800)))
    except ValueError:
        return 800


def max_parallel():
    try:
        return max(1, int(os.environ.get("SUMMARY_MAX_PARALLEL", 3)))
    except ValueError:
        return 3


def parse_srt(path):
    """Parse an .srt into Segments. Returns [] if it can't be read or parsed."""
    try:
        raw = Path(path).read_text(errors="replace")
    except OSError:
        return []

    segments = []
    # Blocks are separated by blank lines: index / timing / text...
    for block in re.split(r"\n\s*\n", raw):
        lines = [ln for ln in block.splitlines() if ln.strip()]
        if len(lines) < 2:
            continue
        timing = next((ln for ln in lines if SRT_TIME.search(ln)), None)
        if timing is None:
            continue
        m = SRT_TIME.search(timing)
        start = (int(m.group(1)) * 3600 + int(m.group(2)) * 60
                 + int(m.group(3)) + int(m.group(4)) / 1000.0)
        end = (int(m.group(5)) * 3600 + int(m.group(6)) * 60
               + int(m.group(7)) + int(m.group(8)) / 1000.0)
        text = " ".join(lines[lines.index(timing) + 1:]).strip()
        if text:
            segments.append(Segment(start, end, text))
    return segments


def find_srt_for(transcript_path):
    """The .srt sibling of a .txt transcript, if the pipeline produced one."""
    p = Path(transcript_path)
    candidate = p.with_suffix(".srt")
    return candidate if candidate.is_file() else None


def should_chunk(transcript, limit=None):
    limit = chunk_chars() if limit is None else limit
    return limit > 0 and len(transcript) > limit


def chunk_by_segments(segments, frames, limit=None, overlap=None):
    """Split timed segments into chunks and attach each chunk's frames.

    A frame belongs to the chunk whose time window contains it, so the model
    sees the slide that was actually on screen for the words it's reading.
    """
    limit = chunk_chars() if limit is None else limit
    overlap = chunk_overlap() if overlap is None else overlap

    chunks = []
    current, current_len = [], 0
    for seg in segments:
        # Split on the segment that would push us over, not mid-sentence.
        if current and current_len + len(seg.text) > limit:
            chunks.append(current)
            # Carry the tail of the previous chunk forward so a thought that
            # straddles the boundary still has its lead-in.
            carried, carried_len = [], 0
            for prev in reversed(current):
                if carried_len >= overlap:
                    break
                carried.insert(0, prev)
                carried_len += len(prev.text)
            current = list(carried)
            current_len = carried_len
        current.append(seg)
        current_len += len(seg.text)
    if current:
        chunks.append(current)

    out = []
    for i, group in enumerate(chunks):
        start_s = group[0].start_s
        end_s = group[-1].end_s
        # The last chunk keeps any frames past its final segment (a trailing
        # slide after the last spoken word would otherwise be dropped).
        is_last = i == len(chunks) - 1
        selected = [
            f for f in frames
            if f.timestamp_s >= start_s and (is_last or f.timestamp_s < end_s)
        ]
        out.append(Chunk(
            index=i,
            text="\n".join(s.text for s in group),
            start_s=start_s,
            end_s=end_s,
            frames=selected,
        ))
    return out


def chunk_by_text(transcript, frames, limit=None, overlap=None):
    """Fallback when there's no .srt: split on line boundaries, share frames out.

    Frames are divided proportionally by position, which is only an
    approximation — but sending all frames to all chunks would multiply the
    image payload by the chunk count.
    """
    limit = chunk_chars() if limit is None else limit
    overlap = chunk_overlap() if overlap is None else overlap

    lines = transcript.splitlines()
    groups, current, current_len = [], [], 0
    for line in lines:
        if current and current_len + len(line) > limit:
            groups.append(current)
            carried, carried_len = [], 0
            for prev in reversed(current):
                if carried_len >= overlap:
                    break
                carried.insert(0, prev)
                carried_len += len(prev)
            current, current_len = list(carried), carried_len
        current.append(line)
        current_len += len(line)
    if current:
        groups.append(current)
    if not groups:
        groups = [[transcript]]

    ordered = sorted(frames, key=lambda f: f.timestamp_s)
    per = max(1, len(ordered) // max(1, len(groups)))
    out = []
    for i, group in enumerate(groups):
        start = i * per
        stop = len(ordered) if i == len(groups) - 1 else (i + 1) * per
        out.append(Chunk(index=i, text="\n".join(group), frames=ordered[start:stop]))
    return out


def build_chunks(transcript, frames, transcript_path=None):
    """Chunk a transcript, preferring the timestamped .srt when available.

    Returns [] when the transcript is short enough to summarize in one call.
    """
    if not should_chunk(transcript):
        return []

    if transcript_path:
        srt = find_srt_for(transcript_path)
        if srt:
            segments = parse_srt(srt)
            if segments:
                return chunk_by_segments(segments, frames)

    return chunk_by_text(transcript, frames)
