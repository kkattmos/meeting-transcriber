#!/usr/bin/env python3
"""
The Discord voice bot's recording format, and the mixer that turns it into
one .m4a for the pipeline.

Discord does not send a voice channel as one stream. Each speaker arrives as
their own Opus stream, only while they are talking (nothing at all during
silence), with no clock shared between speakers. So the bot cannot just
"record the channel": it has to place every speaker's audio on one timeline
itself, and that placement is the part that is easy to get wrong — both
receive libraries' own silence padding is known broken (CLAUDE.md, "A Discord
voice source"). This module is that placement, kept out of the bot so it does
not depend on which Discord library the bot ends up using, and so it can be
tested against synthetic packet timings.

The spool (one directory per recording, written while the call runs):

  session.json     {"version": 1, "started_at": <epoch s>, "sample_rate":
                    48000, "channels": 2, "format": "s16le", ...the bot's own
                    fields (guild, channel, requester)...}; "ended_at_ms" is
                    added when the recording stops.
  users.json       {"<user id>": "<display name>"}
  <user id>.pcm    that speaker's decoded audio, s16le 48 kHz stereo,
                   append-only, silence left out
  <user id>.idx    JSON lines {"t": <ms since the start>, "off": <byte
                   offset in the .pcm>, "n": <bytes>}: bytes [off, off+n) of
                   the .pcm play from t. The .pcm is written and flushed
                   before its index line, so a crash loses at most the last
                   second and never leaves a line pointing past the data.

Placement (SpoolWriter.write): a packet continues its speaker's current
"burst" unless the speaker has been quiet for more than GAP_MS, in which case
a new burst starts at the time the packet arrived. When the library also
hands over the RTP timestamp (discord-ext-voice-recv does; @discordjs/voice
does not), it places the packet inside the burst instead of arrival order, so
a lost packet becomes a gap of the right length rather than pulling every
later word 20 ms early; a timestamp that disagrees with the wall clock by
more than RESYNC_MS (a new SSRC after a reconnect) is ignored and the burst
restarts at arrival time.

Outputs (mix):
  <name>.m4a                 everyone, summed, AAC 128k stereo — the file the
                             pipeline transcribes (same shape as an
                             audio-only Meet recording)
  <name>.speakers/           per-speaker tracks, kept for a later upgrade to
                             named speakers: <user id>.m4a (that speaker's
                             speech only, mono) + speakers.json mapping each
                             stretch of the track back onto the meeting's
                             clock
The spool is deleted only when both outputs exist and probe to the expected
length (--remove-spool). The recording is the one irreplaceable artifact.

CLI:
  discord_spool.py info --spool DIR
  discord_spool.py mix  --spool DIR --out FILE.m4a [--speakers-dir DIR]
                        [--remove-spool]
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

SAMPLE_RATE = 48000
CHANNELS = 2
SAMPLE_BYTES = 2
FRAME_BYTES = CHANNELS * SAMPLE_BYTES      # one sample on every channel
FRAMES_PER_MS = SAMPLE_RATE / 1000         # 48
BYTES_PER_MS = FRAMES_PER_MS * FRAME_BYTES  # 192
RTP_CLOCK_PER_MS = 48                      # Opus RTP always runs at 48 kHz

# A speaker quiet for longer than this has stopped talking; their next packet
# starts a new burst at its own arrival time. Discord sends a packet every
# 20 ms while someone talks, so 200 ms is ten missing packets — well past
# ordinary jitter, well under a pause between sentences.
GAP_MS = 200
# An RTP timestamp further than this from the wall clock is not trusted.
RESYNC_MS = 1000
# Pending audio is written out once a second per speaker.
FLUSH_BYTES = int(BYTES_PER_MS * 1000)

MIXED_BITRATE = "128k"
SPEAKER_BITRATE = "64k"
# AAC encoder priming and the last partial frame: a probe may differ from the
# spool's own length by this much and still be the whole recording.
PROBE_TOLERANCE_S = 1.0

SPOOL_VERSION = 1


class SpoolError(RuntimeError):
    """A spool that cannot be read or mixed. The spool itself is kept."""


def _write_json_atomic(path, data):
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1) + "\n",
                   encoding="utf-8")
    os.replace(tmp, path)


class _Track:
    """One speaker's files and current burst."""

    def __init__(self, spool_dir, user_id):
        self.pcm = open(spool_dir / f"{user_id}.pcm", "ab")
        self.idx = open(spool_dir / f"{user_id}.idx", "a", encoding="utf-8")
        self.off = self.pcm.tell()
        self.burst_t0 = None        # ms since the start
        self.burst_rtp0 = None
        self.burst_frames = 0       # frames placed in this burst so far
        self.seg_frame = 0          # burst frame where `pending` begins
        self.pending = bytearray()

    @property
    def end_ms(self):
        return self.burst_t0 + self.burst_frames / FRAMES_PER_MS

    def flush(self):
        if not self.pending:
            return
        self.pcm.write(self.pending)
        self.pcm.flush()
        t = self.burst_t0 + self.seg_frame / FRAMES_PER_MS
        self.idx.write(json.dumps(
            {"t": round(t, 3), "off": self.off, "n": len(self.pending)}) + "\n")
        self.idx.flush()
        self.off += len(self.pending)
        self.seg_frame = self.burst_frames
        self.pending = bytearray()

    def new_burst(self, t_ms, rtp_ts):
        self.flush()
        self.burst_t0 = t_ms
        self.burst_rtp0 = rtp_ts
        self.burst_frames = 0
        self.seg_frame = 0

    def append(self, pcm):
        self.pending += pcm
        self.burst_frames += len(pcm) // FRAME_BYTES
        if len(self.pending) >= FLUSH_BYTES:
            self.flush()

    def close(self):
        self.flush()
        self.pcm.close()
        self.idx.close()


class SpoolWriter:
    """Writes the spool while a call is recorded. Thread-safe.

    The receive libraries call their sinks from a reader thread, and the bot
    renames users from its event loop, so every public method takes the lock.
    `clock` / `wall` are there for the tests.
    """

    def __init__(self, spool_dir, *, meta=None, clock=time.monotonic,
                 wall=time.time):
        self.dir = Path(spool_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self._clock = clock
        self._t0 = clock()
        self._lock = threading.Lock()
        self._tracks = {}
        self._names = {}
        self._closed = False
        self.session = dict(meta or {})
        self.session.update(version=SPOOL_VERSION, started_at=wall(),
                            sample_rate=SAMPLE_RATE, channels=CHANNELS,
                            format="s16le")
        _write_json_atomic(self.dir / "session.json", self.session)
        _write_json_atomic(self.dir / "users.json", self._names)

    def now_ms(self):
        return (self._clock() - self._t0) * 1000.0

    def set_name(self, user_id, name):
        with self._lock:
            key = str(user_id)
            if self._names.get(key) == name:
                return
            self._names[key] = name
            _write_json_atomic(self.dir / "users.json", self._names)

    def write(self, user_id, pcm, *, arrival_ms=None, rtp_ts=None):
        """Place one decoded packet of `user_id`'s audio on the timeline."""
        if not pcm:
            return
        pcm = bytes(pcm[:len(pcm) - len(pcm) % FRAME_BYTES])
        with self._lock:
            if self._closed:
                return
            arrival = self.now_ms() if arrival_ms is None else float(arrival_ms)
            key = str(user_id)
            track = self._tracks.get(key)
            if track is None:
                track = self._tracks[key] = _Track(self.dir, key)
            self._place(track, pcm, arrival, rtp_ts)

    @staticmethod
    def _place(track, pcm, arrival, rtp_ts):
        if track.burst_t0 is None:
            track.new_burst(arrival, rtp_ts)
            track.append(pcm)
            return
        if rtp_ts is not None and track.burst_rtp0 is not None:
            diff = (int(rtp_ts) - track.burst_rtp0) & 0xFFFFFFFF
            if diff >= 1 << 31:       # behind the burst's start (wrap-aware)
                diff -= 1 << 32
            target = track.burst_t0 + diff / RTP_CLOCK_PER_MS
            if abs(target - arrival) > RESYNC_MS:
                track.new_burst(arrival, rtp_ts)
            elif target - track.end_ms > GAP_MS:
                # A pause. Keep the RTP clock (it is the better one) but do
                # not store the silence: start a new burst where it resumes.
                track.new_burst(target, rtp_ts)
            else:
                gap_frames = round((target - track.end_ms) * FRAMES_PER_MS)
                if gap_frames > 0:
                    # Lost packets the library did not conceal: keep the
                    # words after them where they were spoken.
                    track.append(bytes(gap_frames * FRAME_BYTES))
            track.append(pcm)
            return
        if arrival - track.end_ms > GAP_MS:
            track.new_burst(arrival, rtp_ts)
        track.append(pcm)

    def flush(self):
        with self._lock:
            for track in self._tracks.values():
                track.flush()

    def close(self):
        """Flush everything and stamp the session's length. Idempotent."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            for track in self._tracks.values():
                track.close()
            self.session["ended_at_ms"] = round(self.now_ms(), 3)
            _write_json_atomic(self.dir / "session.json", self.session)


# --- Reading -----------------------------------------------------------------

class Segment:
    __slots__ = ("user", "t_ms", "off", "n")

    def __init__(self, user, t_ms, off, n):
        self.user, self.t_ms, self.off, self.n = user, t_ms, off, n

    @property
    def start_frame(self):
        return round(self.t_ms * FRAMES_PER_MS)

    @property
    def frames(self):
        return self.n // FRAME_BYTES

    @property
    def end_frame(self):
        return self.start_frame + self.frames


def read_spool(spool_dir):
    """(session, names, {user: [Segment]}) — tolerant of a crash mid-write."""
    spool_dir = Path(spool_dir)
    try:
        session = json.loads((spool_dir / "session.json").read_text("utf-8"))
    except (OSError, ValueError) as exc:
        raise SpoolError(f"{spool_dir}: no readable session.json ({exc})")
    if session.get("version") != SPOOL_VERSION:
        raise SpoolError(f"{spool_dir}: spool version {session.get('version')!r}, "
                         f"this reader knows {SPOOL_VERSION}")
    if (session.get("sample_rate"), session.get("channels"),
            session.get("format")) != (SAMPLE_RATE, CHANNELS, "s16le"):
        raise SpoolError(f"{spool_dir}: unexpected audio format in session.json")
    try:
        names = json.loads((spool_dir / "users.json").read_text("utf-8"))
    except (OSError, ValueError):
        names = {}

    tracks = {}
    for idx in sorted(spool_dir.glob("*.idx")):
        user = idx.stem
        pcm = spool_dir / f"{user}.pcm"
        size = pcm.stat().st_size if pcm.exists() else 0
        segs = []
        for line in idx.read_text("utf-8").splitlines():
            try:
                rec = json.loads(line)
                seg = Segment(user, float(rec["t"]), int(rec["off"]), int(rec["n"]))
            except (ValueError, KeyError, TypeError):
                continue    # a line cut short by a crash
            seg.n = min(seg.n, max(0, size - seg.off))
            seg.n -= seg.n % FRAME_BYTES
            if seg.n > 0 and seg.t_ms >= 0:
                segs.append(seg)
        if segs:
            tracks[user] = sorted(segs, key=lambda s: s.t_ms)
    return session, names, tracks


def total_frames(session, tracks):
    """The recording's length: to the last word, or to the stop if later."""
    end = max((s.end_frame for segs in tracks.values() for s in segs), default=0)
    ended = session.get("ended_at_ms")
    if ended is not None:
        end = max(end, round(float(ended) * FRAMES_PER_MS))
    return end


def info(spool_dir):
    session, names, tracks = read_spool(spool_dir)
    frames = total_frames(session, tracks)
    return {
        "duration_s": round(frames / SAMPLE_RATE, 3),
        "speakers": {
            user: {"name": names.get(user, ""),
                   "segments": len(segs),
                   "speech_s": round(sum(s.frames for s in segs) / SAMPLE_RATE, 3)}
            for user, segs in tracks.items()},
    }


# --- Mixing ------------------------------------------------------------------

def _numpy():
    try:
        import numpy
    except ImportError:
        raise SpoolError("numpy is required to mix a Discord recording "
                         "(it is pinned in requirements.txt; re-run ./setup.sh)")
    return numpy


def mixed_blocks(spool_dir, block_frames=SAMPLE_RATE * 10):
    """Yield the summed timeline as s16le bytes, one block at a time.

    Summed in int32 and clipped once, so two loud speakers saturate rather
    than wrap around. Memory is one block, whatever the call's length.
    """
    np = _numpy()
    session, _names, tracks = read_spool(spool_dir)
    spool_dir = Path(spool_dir)
    total = total_frames(session, tracks)
    segs = sorted((s for ss in tracks.values() for s in ss),
                  key=lambda s: s.start_frame)
    files = {u: open(spool_dir / f"{u}.pcm", "rb") for u in tracks}
    try:
        nxt = 0
        active = []
        for b0 in range(0, total, block_frames):
            b1 = min(b0 + block_frames, total)
            while nxt < len(segs) and segs[nxt].start_frame < b1:
                active.append(segs[nxt])
                nxt += 1
            active = [s for s in active if s.end_frame > b0]
            acc = np.zeros((b1 - b0) * CHANNELS, dtype=np.int32)
            for seg in active:
                s = max(seg.start_frame, b0)
                e = min(seg.end_frame, b1)
                if e <= s:
                    continue
                fh = files[seg.user]
                fh.seek(seg.off + (s - seg.start_frame) * FRAME_BYTES)
                data = fh.read((e - s) * FRAME_BYTES)
                got = len(data) // FRAME_BYTES
                if got:
                    acc[(s - b0) * CHANNELS:(s - b0 + got) * CHANNELS] += \
                        np.frombuffer(data[:got * FRAME_BYTES], dtype="<i2")
            np.clip(acc, -32768, 32767, out=acc)
            yield acc.astype("<i2").tobytes()
    finally:
        for fh in files.values():
            fh.close()


def _ffmpeg():
    return os.environ.get("FFMPEG_BIN") or shutil.which("ffmpeg") or "ffmpeg"


def _ffprobe():
    return os.environ.get("FFPROBE_BIN") or shutil.which("ffprobe") or "ffprobe"


def _pcm_input_args():
    return ["-f", "s16le", "-ar", str(SAMPLE_RATE), "-ac", str(CHANNELS)]


def _part_path(out):
    # ffmpeg picks its muxer from the extension: "x.m4a.part" makes it refuse
    # to start (the same lesson as clip.part.mp4).
    return out.with_name(out.stem + ".part" + out.suffix)


def probe_duration(path):
    try:
        out = subprocess.run(
            [_ffprobe(), "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nw=1:nk=1", str(path)],
            capture_output=True, text=True, check=True).stdout.strip()
        return float(out)
    except (OSError, subprocess.CalledProcessError, ValueError):
        return None


def _check_length(path, expected_s):
    got = probe_duration(path)
    if got is None:
        raise SpoolError(f"{path}: ffprobe cannot read the file")
    if abs(got - expected_s) > PROBE_TOLERANCE_S:
        raise SpoolError(f"{path}: {got:.2f}s long, expected {expected_s:.2f}s")
    return got


def mix(spool_dir, out, *, bitrate=MIXED_BITRATE):
    """Write the summed .m4a; returns its probed duration in seconds."""
    out = Path(out)
    session, _names, tracks = read_spool(spool_dir)
    if not tracks:
        raise SpoolError(f"{spool_dir}: nobody spoke — no audio to mix")
    expected = total_frames(session, tracks) / SAMPLE_RATE
    out.parent.mkdir(parents=True, exist_ok=True)
    part = _part_path(out)
    cmd = [_ffmpeg(), "-hide_banner", "-loglevel", "error", "-y",
           *_pcm_input_args(), "-i", "pipe:0",
           "-c:a", "aac", "-b:a", bitrate, "-movflags", "+faststart", str(part)]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE)
    try:
        for block in mixed_blocks(spool_dir):
            proc.stdin.write(block)
    except BrokenPipeError:
        pass
    finally:
        proc.stdin.close()
        rc = proc.wait()
    if rc != 0:
        part.unlink(missing_ok=True)
        raise SpoolError(f"ffmpeg exited {rc} while writing {out}")
    got = _check_length(part, expected)
    os.replace(part, out)
    return got


def export_speakers(spool_dir, out_dir, *, bitrate=SPEAKER_BITRATE):
    """One speech-only mono track per speaker + speakers.json.

    A track is the speaker's .pcm as written — their bursts back to back, no
    silence — so transcribing it bills only their speaking time. speakers.json
    maps each stretch of the track back to the meeting's clock:
    meeting time = start + (track time - track_start).
    """
    spool_dir, out_dir = Path(spool_dir), Path(out_dir)
    session, names, tracks = read_spool(spool_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    speakers = {}
    for user, segs in tracks.items():
        track = out_dir / f"{user}.m4a"
        part = _part_path(track)
        length = max(s.off + s.n for s in segs)
        # Only the indexed bytes: a crash may have left an unindexed tail.
        cmd = [_ffmpeg(), "-hide_banner", "-loglevel", "error", "-y",
               *_pcm_input_args(), "-i", "pipe:0", "-ac", "1",
               "-c:a", "aac", "-b:a", bitrate, "-movflags", "+faststart",
               str(part)]
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE)
        try:
            with open(spool_dir / f"{user}.pcm", "rb") as fh:
                remaining = length
                while remaining > 0:
                    chunk = fh.read(min(remaining, FLUSH_BYTES * 10))
                    if not chunk:
                        break
                    proc.stdin.write(chunk)
                    remaining -= len(chunk)
        except BrokenPipeError:
            pass
        finally:
            proc.stdin.close()
            rc = proc.wait()
        if rc != 0:
            part.unlink(missing_ok=True)
            raise SpoolError(f"ffmpeg exited {rc} while writing {track}")
        _check_length(part, length / FRAME_BYTES / SAMPLE_RATE)
        os.replace(part, track)
        speakers[user] = {
            "name": names.get(user, ""),
            "track": track.name,
            "segments": [
                {"start": round(s.t_ms / 1000, 3),
                 "track_start": round(s.off / FRAME_BYTES / SAMPLE_RATE, 3),
                 "duration": round(s.frames / SAMPLE_RATE, 3)}
                for s in segs],
        }
    meta = {k: v for k, v in session.items()
            if k not in ("sample_rate", "channels", "format")}
    _write_json_atomic(out_dir / "speakers.json",
                       {"version": SPOOL_VERSION, "session": meta,
                        "speakers": speakers})
    return speakers


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("info")
    p.add_argument("--spool", required=True)
    p = sub.add_parser("mix")
    p.add_argument("--spool", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--speakers-dir")
    p.add_argument("--remove-spool", action="store_true",
                   help="delete the spool once every output probed correctly")
    args = ap.parse_args(argv)
    try:
        if args.cmd == "info":
            print(json.dumps(info(args.spool), ensure_ascii=False, indent=1))
            return 0
        result = {"out": str(Path(args.out).resolve()),
                  "duration_s": round(mix(args.spool, args.out), 3)}
        if args.speakers_dir:
            speakers = export_speakers(args.spool, args.speakers_dir)
            result["speakers_dir"] = str(Path(args.speakers_dir).resolve())
            result["speakers"] = len(speakers)
        if args.remove_spool:
            shutil.rmtree(args.spool)
            result["spool_removed"] = True
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except SpoolError as exc:
        print(f"discord_spool: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
