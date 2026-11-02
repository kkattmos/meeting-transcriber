// The JS twin of lib/discord_spool.py's SpoolWriter, arrival-time placement
// only (@discordjs/voice gives no RTP timestamps). Same files, same format:
// lib/discord_spool.py reads and mixes what this writes.
import fs from 'node:fs';
import path from 'node:path';

const FRAME_BYTES = 4;              // s16le stereo
const GAP_MS = 200;                 // same constants as the Python writer
const FLUSH_BYTES = 192000;

export class SpoolWriter {
  constructor(dir, meta) {
    this.dir = dir;
    fs.mkdirSync(dir, { recursive: true });
    this.t0 = performance.now();
    this.tracks = new Map();
    this.names = {};
    this.session = { ...meta, version: 1, started_at: Date.now() / 1000,
                     sample_rate: 48000, channels: 2, format: 's16le' };
    this.writeJson('session.json', this.session);
    this.writeJson('users.json', this.names);
  }
  writeJson(name, data) {
    const p = path.join(this.dir, name);
    fs.writeFileSync(p + '.tmp', JSON.stringify(data, null, 1) + '\n');
    fs.renameSync(p + '.tmp', p);
  }
  nowMs() { return performance.now() - this.t0; }
  setName(id, name) {
    if (this.names[id] === name) return;
    this.names[id] = name;
    this.writeJson('users.json', this.names);
  }
  track(id) {
    let t = this.tracks.get(id);
    if (!t) {
      const pcmPath = path.join(this.dir, `${id}.pcm`);
      t = { pcm: fs.openSync(pcmPath, 'a'), idx: fs.openSync(path.join(this.dir, `${id}.idx`), 'a'),
            off: fs.existsSync(pcmPath) ? fs.statSync(pcmPath).size : 0,
            t0: null, frames: 0, segFrame: 0, pending: [] , pendingBytes: 0 };
      this.tracks.set(id, t);
    }
    return t;
  }
  flushTrack(t) {
    if (!t.pendingBytes) return;
    const buf = Buffer.concat(t.pending);
    fs.writeSync(t.pcm, buf);
    fs.fsyncSync(t.pcm);
    const tMs = t.t0 + t.segFrame / 48;
    fs.writeSync(t.idx, JSON.stringify({ t: Math.round(tMs * 1000) / 1000, off: t.off, n: buf.length }) + '\n');
    t.off += buf.length;
    t.segFrame = t.frames;
    t.pending = []; t.pendingBytes = 0;
  }
  write(id, pcm, arrivalMs = this.nowMs()) {
    if (this.closed || !pcm.length) return;
    pcm = pcm.subarray(0, pcm.length - (pcm.length % FRAME_BYTES));
    const t = this.track(id);
    const endMs = t.t0 === null ? null : t.t0 + t.frames / 48;
    if (t.t0 === null || arrivalMs - endMs > GAP_MS) {
      this.flushTrack(t);
      t.t0 = arrivalMs; t.frames = 0; t.segFrame = 0;
    }
    t.pending.push(Buffer.from(pcm));
    t.pendingBytes += pcm.length;
    t.frames += pcm.length / FRAME_BYTES;
    if (t.pendingBytes >= FLUSH_BYTES) this.flushTrack(t);
  }
  close() {
    if (this.closed) return;
    this.closed = true;
    for (const t of this.tracks.values()) {
      this.flushTrack(t);
      fs.closeSync(t.pcm); fs.closeSync(t.idx);
    }
    this.session.ended_at_ms = Math.round(this.nowMs() * 1000) / 1000;
    this.writeJson('session.json', this.session);
  }
}
