#!/usr/bin/env python3
"""Unit tests for lib/discord_spool.py — synthetic packet timings, no Discord.

What matters is where each speaker's audio lands on the shared timeline, so
most tests feed the writer 20 ms packets with chosen arrival times and RTP
timestamps and assert on the index it wrote. The last class runs the real
ffmpeg (skipped without one) and decodes the mix back to check the words are
where they were spoken.

    python3 lib/test_discord_spool.py
"""
import json
import math
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import discord_spool as ds  # noqa: E402

PACKET_MS = 20
PACKET_FRAMES = 960
PACKET_BYTES = PACKET_FRAMES * ds.FRAME_BYTES


def packet(value=1000):
    """20 ms of a constant sample on both channels."""
    return struct.pack("<h", value) * (PACKET_FRAMES * ds.CHANNELS)


def tone(ms, freq=440, amp=8000, rate=ds.SAMPLE_RATE):
    frames = int(ms * rate / 1000)
    out = bytearray()
    for i in range(frames):
        v = int(amp * math.sin(2 * math.pi * freq * i / rate))
        out += struct.pack("<hh", v, v)
    return bytes(out)


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="discord spool ")
        self.dir = Path(self._tmp.name) / "spool"
        self.now = 0.0
        self.w = ds.SpoolWriter(self.dir, meta={"guild_id": "1"},
                                clock=lambda: self.now, wall=lambda: 1.7e9)

    def tearDown(self):
        self._tmp.cleanup()

    def segments(self, user="u1"):
        self.w.close()
        _s, _n, tracks = ds.read_spool(self.dir)
        return [(s.t_ms, s.off, s.n) for s in tracks.get(user, [])]


class ArrivalOnly(Base):
    """@discordjs/voice: no RTP timestamps, arrival time is all there is."""

    def test_continuous_speech_is_one_stretch_at_its_arrival(self):
        for i in range(100):
            self.w.write("u1", packet(), arrival_ms=500 + i * PACKET_MS)
        segs = self.segments()
        self.assertEqual(segs[0][0], 500)
        self.assertEqual(sum(n for _t, _o, n in segs), 100 * PACKET_BYTES)
        # Contiguous: each index line starts where the previous one ended.
        for (t1, o1, n1), (t2, o2, _n2) in zip(segs, segs[1:]):
            self.assertAlmostEqual(t2, t1 + n1 / ds.BYTES_PER_MS, places=3)
            self.assertEqual(o2, o1 + n1)

    def test_jitter_under_the_gap_does_not_split_a_burst(self):
        for i in range(10):
            self.w.write("u1", packet(), arrival_ms=i * PACKET_MS + (60 if i % 2 else 0))
        self.assertEqual(len(self.segments()), 1)

    def test_a_pause_starts_a_new_burst_and_stores_no_silence(self):
        for i in range(10):
            self.w.write("u1", packet(), arrival_ms=i * PACKET_MS)
        for i in range(10):
            self.w.write("u1", packet(), arrival_ms=5000 + i * PACKET_MS)
        segs = self.segments()
        self.assertEqual([t for t, _o, _n in segs], [0, 5000])
        self.assertEqual((self.dir / "u1.pcm").stat().st_size, 20 * PACKET_BYTES)

    def test_default_arrival_is_the_writer_clock(self):
        self.now = 2.5
        self.w.write("u1", packet())
        self.assertEqual(self.segments()[0][0], 2500)

    def test_a_ragged_packet_is_trimmed_to_whole_frames(self):
        self.w.write("u1", packet() + b"\x01", arrival_ms=0)
        self.w.close()
        self.assertEqual((self.dir / "u1.pcm").stat().st_size, PACKET_BYTES)


class WithRtp(Base):
    """discord-ext-voice-recv: every packet carries its RTP timestamp."""

    def feed(self, rtp0, arrivals, lose=(), user="u1", rtp_step=PACKET_FRAMES):
        for i, arrival in enumerate(arrivals):
            if i in lose:
                continue
            self.w.write(user, packet(), arrival_ms=arrival,
                         rtp_ts=(rtp0 + i * rtp_step) & 0xFFFFFFFF)

    def test_bunched_arrivals_are_placed_by_rtp(self):
        # All ten packets arrive at once (a jitter-buffer release): RTP spaces
        # them out, contiguous from the first.
        self.feed(1000, [100] * 10)
        segs = self.segments()
        self.assertEqual(segs[0][0], 100)
        self.assertEqual(sum(n for _t, _o, n in segs), 10 * PACKET_BYTES)

    def test_a_lost_packet_leaves_a_gap_of_its_length(self):
        self.feed(0, [i * PACKET_MS for i in range(10)], lose={4})
        self.w.close()
        size = (self.dir / "u1.pcm").stat().st_size
        self.assertEqual(size, 10 * PACKET_BYTES)   # 9 packets + 20 ms of zeros
        data = (self.dir / "u1.pcm").read_bytes()
        hole = data[4 * PACKET_BYTES:5 * PACKET_BYTES]
        self.assertEqual(hole, bytes(PACKET_BYTES))
        self.assertEqual(data[5 * PACKET_BYTES:6 * PACKET_BYTES], packet())

    def test_rtp_wraparound_is_continuous(self):
        self.feed(0xFFFFFFFF - 3 * PACKET_FRAMES, [i * PACKET_MS for i in range(8)])
        segs = self.segments()
        self.assertEqual(sum(n for _t, _o, n in segs), 8 * PACKET_BYTES)
        self.assertEqual(segs[0][0], 0)
        self.assertEqual(len({o + n for _t, o, n in segs}), len(segs))

    def test_a_pause_on_the_rtp_clock_is_not_stored(self):
        # The sender's RTP clock kept running through 3 s of silence.
        self.feed(0, [i * PACKET_MS for i in range(5)])
        base = 3000
        for i in range(5):
            self.w.write("u1", packet(), arrival_ms=base + i * PACKET_MS + 30,
                         rtp_ts=(base + i * PACKET_MS) * ds.RTP_CLOCK_PER_MS)
        segs = self.segments()
        self.assertEqual([round(t) for t, _o, _n in segs], [0, 3000])
        self.assertEqual((self.dir / "u1.pcm").stat().st_size, 10 * PACKET_BYTES)

    def test_an_rtp_clock_that_disagrees_with_the_wall_is_ignored(self):
        # A reconnect: new SSRC, a random new RTP origin.
        self.feed(0, [i * PACKET_MS for i in range(5)])
        self.w.write("u1", packet(), arrival_ms=5000, rtp_ts=123456789)
        self.w.write("u1", packet(), arrival_ms=5020, rtp_ts=123456789 + PACKET_FRAMES)
        segs = self.segments()
        self.assertEqual([round(t) for t, _o, _n in segs], [0, 5000])
        self.assertEqual(sum(n for _t, _o, n in segs[1:]), 2 * PACKET_BYTES)

    def test_speakers_have_their_own_tracks(self):
        self.feed(0, [i * PACKET_MS for i in range(3)], user="a")
        self.feed(99, [700 + i * PACKET_MS for i in range(2)], user="b")
        self.w.set_name("a", "Alice")
        self.w.close()
        session, names, tracks = ds.read_spool(self.dir)
        self.assertEqual(set(tracks), {"a", "b"})
        self.assertEqual(names, {"a": "Alice"})
        self.assertEqual(tracks["b"][0].t_ms, 700)
        self.assertEqual(session["guild_id"], "1")


class Reading(Base):
    def test_a_crash_mid_line_loses_only_that_line(self):
        for i in range(60):          # > 1 s, so at least one flush
            self.w.write("u1", packet(), arrival_ms=i * PACKET_MS)
        self.w.close()
        with open(self.dir / "u1.idx", "a") as fh:
            fh.write('{"t": 9999, "off": 1')
        _s, _n, tracks = ds.read_spool(self.dir)
        self.assertEqual(sum(s.n for s in tracks["u1"]), 60 * PACKET_BYTES)

    def test_an_index_past_the_data_is_clamped(self):
        self.w.write("u1", packet(), arrival_ms=0)
        self.w.close()
        with open(self.dir / "u1.idx", "a") as fh:
            fh.write(json.dumps({"t": 100, "off": PACKET_BYTES, "n": PACKET_BYTES}) + "\n")
        _s, _n, tracks = ds.read_spool(self.dir)
        self.assertEqual(len(tracks["u1"]), 1)

    def test_the_length_runs_to_the_stop(self):
        self.w.write("u1", packet(), arrival_ms=0)
        self.now = 10.0
        self.w.close()
        session, _n, tracks = ds.read_spool(self.dir)
        self.assertEqual(ds.total_frames(session, tracks), 10 * ds.SAMPLE_RATE)

    def test_an_unknown_version_is_refused(self):
        self.w.close()
        data = json.loads((self.dir / "session.json").read_text())
        data["version"] = 99
        (self.dir / "session.json").write_text(json.dumps(data))
        with self.assertRaises(ds.SpoolError):
            ds.read_spool(self.dir)

    def test_writes_after_close_are_dropped(self):
        self.w.close()
        self.w.write("u1", packet(), arrival_ms=0)
        self.assertFalse((self.dir / "u1.pcm").exists())


class Mixing(Base):
    def setUp(self):
        super().setUp()
        try:
            import numpy  # noqa: F401
        except ImportError:
            self.skipTest("numpy not installed")

    def mixed(self):
        self.w.close()
        return b"".join(ds.mixed_blocks(self.dir, block_frames=1000))

    def sample_at(self, data, ms):
        frame = int(ms * ds.FRAMES_PER_MS)
        return struct.unpack_from("<h", data, frame * ds.FRAME_BYTES)[0]

    def test_speakers_land_at_their_times_and_overlaps_add(self):
        self.w.write("a", packet(1000), arrival_ms=100)
        self.w.write("b", packet(300), arrival_ms=110)
        data = self.mixed()
        self.assertEqual(self.sample_at(data, 50), 0)
        self.assertEqual(self.sample_at(data, 105), 1000)
        self.assertEqual(self.sample_at(data, 115), 1300)
        self.assertEqual(self.sample_at(data, 125), 300)
        self.assertEqual(len(data), round(130 * ds.BYTES_PER_MS))

    def test_loud_overlaps_saturate_instead_of_wrapping(self):
        self.w.write("a", packet(30000), arrival_ms=0)
        self.w.write("b", packet(30000), arrival_ms=0)
        self.assertEqual(self.sample_at(self.mixed(), 10), 32767)


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "no ffmpeg")
class RealFfmpeg(Base):
    def setUp(self):
        super().setUp()
        try:
            import numpy  # noqa: F401
        except ImportError:
            self.skipTest("numpy not installed")
        # Alice speaks 1.0-2.0 s, Bob 3.0-3.5 s, the recording stops at 5 s.
        self._write("alice", tone(1000, 440), 1000)
        self._write("bob", tone(500, 660), 3000)
        self.w.set_name("alice", "Alice")
        self.w.set_name("bob", "Bob")
        self.now = 5.0
        self.w.close()
        self.out = Path(self._tmp.name) / "out dir" / "call.m4a"

    def _write(self, user, pcm, start_ms):
        for i in range(0, len(pcm), PACKET_BYTES):
            self.w.write(user, pcm[i:i + PACKET_BYTES],
                         arrival_ms=start_ms + i / ds.BYTES_PER_MS)

    def rms(self, path, start_s, end_s):
        raw = subprocess.run(
            ["ffmpeg", "-v", "error", "-i", str(path), "-f", "s16le", "-ac", "1",
             "-ar", "48000", "-"], capture_output=True, check=True).stdout
        a, b = int(start_s * 48000) * 2, int(end_s * 48000) * 2
        vals = struct.unpack(f"<{(b - a) // 2}h", raw[a:b])
        return math.sqrt(sum(v * v for v in vals) / len(vals))

    def test_the_mix_is_the_whole_call_with_words_in_place(self):
        dur = ds.mix(self.dir, self.out)
        self.assertAlmostEqual(dur, 5.0, delta=ds.PROBE_TOLERANCE_S)
        self.assertLess(self.rms(self.out, 0.2, 0.8), 50)
        self.assertGreater(self.rms(self.out, 1.2, 1.8), 2000)
        self.assertLess(self.rms(self.out, 2.2, 2.8), 50)
        self.assertGreater(self.rms(self.out, 3.1, 3.4), 2000)
        self.assertLess(self.rms(self.out, 4.0, 4.9), 50)
        self.assertFalse(list(self.out.parent.glob("*.part.*")))

    def test_speaker_tracks_hold_speech_only_and_map_back(self):
        sp_dir = Path(self._tmp.name) / "call.speakers"
        speakers = ds.export_speakers(self.dir, sp_dir)
        meta = json.loads((sp_dir / "speakers.json").read_text())
        self.assertEqual(meta["speakers"]["alice"]["name"], "Alice")
        bob = meta["speakers"]["bob"]
        self.assertAlmostEqual(bob["segments"][0]["start"], 3.0, places=2)
        self.assertEqual(bob["segments"][0]["track_start"], 0)
        self.assertAlmostEqual(ds.probe_duration(sp_dir / "bob.m4a"), 0.5, delta=0.1)
        self.assertEqual(set(speakers), {"alice", "bob"})

    def test_cli_removes_the_spool_only_after_success(self):
        rc = ds.main(["mix", "--spool", str(self.dir), "--out", str(self.out),
                      "--speakers-dir", str(self.out.with_suffix(".speakers")),
                      "--remove-spool"])
        self.assertEqual(rc, 0)
        self.assertFalse(self.dir.exists())
        self.assertTrue(self.out.exists())

    def test_nobody_spoke_keeps_the_spool_and_fails(self):
        empty = Path(self._tmp.name) / "empty"
        ds.SpoolWriter(empty).close()
        rc = ds.main(["mix", "--spool", str(empty), "--out", str(self.out),
                      "--remove-spool"])
        self.assertEqual(rc, 1)
        self.assertTrue(empty.exists())


if __name__ == "__main__":
    unittest.main()
