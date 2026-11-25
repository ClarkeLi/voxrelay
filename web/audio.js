// 采集、断句、播放队列。不碰 DOM。
export const RATE = 16000;
export const FRAME = 480; // 30 ms
const MS = 1000 * FRAME / RATE;

export function frameDb(f) {
  let s = 0;
  for (let i = 0; i < f.length; i++) s += f[i] * f[i];
  return Math.max(-80, 10 * Math.log10(s / f.length + 1e-12));
}

export function toPcm16(frames) {
  const out = new Int16Array(frames.length * FRAME);
  frames.forEach((f, k) => {
    for (let i = 0; i < f.length; i++) out[k * FRAME + i] = Math.max(-32768, Math.min(32767, Math.round(f[i] * 32767)));
  });
  return out;
}

export class Capture {
  async start(stream, onFrame) {
    this.stream = stream;
    this.ctx = new AudioContext({ sampleRate: RATE }); // 由浏览器完成重采样
    await this.ctx.audioWorklet.addModule("/static/pcm-worklet.js");
    await this.ctx.resume();
    this.node = new AudioWorkletNode(this.ctx, "pcm-frames", { processorOptions: { size: FRAME } });
    this.node.port.onmessage = e => onFrame(e.data);
    const mute = this.ctx.createGain();
    mute.gain.value = 0; // 接到输出只是为了让节点持续运转，不出声
    this.ctx.createMediaStreamSource(stream).connect(this.node).connect(mute).connect(this.ctx.destination);
  }
  stop() {
    if (this.node) this.node.port.onmessage = null;
    this.stream?.getTracks().forEach(t => t.stop());
    this.ctx?.close();
    this.ctx = this.node = this.stream = null;
  }
}

// 断句：自适应底噪 + 阈值。mode = "auto"（停顿自动结束）或 "ptt"（按住说话）。
// 开启分段（chunking）时，长句边说边切：说够一小段后遇到短停顿就先发出去；一直不停顿则到上限时在最安静处切开。
const CHUNK_MIN_MS = 1500; // 短停顿处切段前，这一段至少要有这么多语音
export class Segmenter {
  constructor(opts, on) {
    this.opts = opts; // () => ({mode, silenceMs, sensitivity, chunking, pauseMs, maxChunk})
    this.on = on;     // {start(), end(frames, info), discard()}
    this.uid = 0;     // 一次连续说话的编号，同一句切出的各段共用
    this.reset();
  }
  reset() {
    this.floor = -60;
    this.threshold = -45;
    this.pre = [];
    this.preDb = [];
    this.run = 0;
    this.seg = null;
  }
  get active() { return !!this.seg; }
  begin(preFrames, voiced) {
    this.seg = { uid: ++this.uid, part: 0, frames: this.pre.slice(-preFrames), dbs: this.preDb.slice(-preFrames), voiced, silent: 0, tail: -1, lastVoice: performance.now() };
    this.seg.start = performance.now() - this.seg.frames.length * MS;
    this.clearPre();
    this.on.start();
  }
  clearPre() { this.pre = []; this.preDb = []; this.run = 0; }
  push(frame) {
    const db = frameDb(frame);
    const { mode, silenceMs, sensitivity, chunking, pauseMs, maxChunk } = this.opts();
    // 底噪向下跟得快、向上跟得慢；说话时几乎冻结
    if (db < this.floor) this.floor = 0.7 * this.floor + 0.3 * db;
    else this.floor += Math.min(db - this.floor, this.seg ? 0.01 : 0.05);
    this.threshold = Math.max(-55, Math.min(-15, this.floor + 24 - 0.18 * sensitivity));
    const s = this.seg;
    if (!s) {
      this.pre.push(frame); this.preDb.push(db);
      if (this.pre.length > 12) { this.pre.shift(); this.preDb.shift(); }
      if (mode !== "auto") return db;
      this.run = db > this.threshold ? this.run + 1 : 0;
      if (this.run >= 3) this.begin(12, this.run);
      return db;
    }
    s.frames.push(frame); s.dbs.push(db);
    if (db > this.threshold - 3) { s.voiced++; s.silent = 0; s.lastVoice = performance.now(); }
    else s.silent++;
    if (s.tail > 0 && --s.tail === 0) this.finish();
    else if (mode === "auto" && s.silent * MS >= silenceMs) this.finish();
    else if (chunking) {
      if (s.silent === Math.ceil(pauseMs / MS) && s.voiced * MS >= CHUNK_MIN_MS) this.cut(s.frames.length - Math.max(0, s.silent - 5), false);
      else if (s.frames.length * MS >= maxChunk * 1000 && s.voiced * MS >= CHUNK_MIN_MS) {
        if (s.silent >= 3) this.cut(s.frames.length - Math.max(0, s.silent - 5), false); // 到上限时正好停着，按停顿切
        else this.cut(this.quietest(), true);
      }
      else if (s.frames.length * MS >= 15000) this.finish();
    } else if (s.frames.length * MS >= 15000) this.finish();
    return db;
  }
  // 最近 1.5 秒里最安静的位置（5 帧平滑），尽量不切在字中间
  quietest() {
    const d = this.seg.dbs, end = d.length - 4;
    let best = end, low = Infinity;
    for (let i = Math.max(10, d.length - 50); i <= end; i++) {
      const v = (d[i - 2] + d[i - 1] + d[i] + d[i + 1] + d[i + 2]) / 5;
      if (v < low) { low = v; best = i; }
    }
    return best;
  }
  // 发出前 k 帧，余下的作为同一句的下一段继续收
  cut(k, forced) {
    const s = this.seg;
    const head = s.frames.slice(0, k), part = s.part++;
    const speechEnd = forced ? performance.now() - (s.frames.length - k) * MS : s.lastVoice;
    s.frames = s.frames.slice(k); s.dbs = s.dbs.slice(k);
    s.voiced = s.dbs.filter(v => v > this.threshold - 3).length;
    this.on.end(head, { uid: s.uid, part, final: false, forced, speechEnd, start: s.start });
    this.on.start();
  }
  press() {
    if (!this.seg) this.begin(5, 0);
    else this.seg.tail = -1;
  }
  release() {
    if (this.seg && this.seg.tail < 0) this.seg.tail = 5; // 再收 150 ms，避免吃掉尾字
  }
  finish() {
    const s = this.seg;
    if (!s) return;
    this.seg = null;
    const min = s.part || this.opts().mode !== "auto" ? 150 : 250;
    if (s.voiced * MS < min) return this.on.discard();
    const keep = s.frames.length - Math.max(0, s.silent - 7); // 尾部静音只留约 200 ms
    this.on.end(s.frames.slice(0, keep), { uid: s.uid, part: s.part, final: true, forced: false, speechEnd: s.lastVoice, start: s.start });
  }
  cancel() {
    if (!this.seg) return false;
    this.seg = null;
    this.on.discard();
    return true;
  }
}

// 播放队列：按顺序播放，数据边到边播。
// job = {chunks: Int16Array[], sr, complete(), ready(), dead(), onStart(), onEnd()}
export class Player {
  constructor(canStart) {
    this.canStart = canStart;
    this.queue = [];
    this.cur = null;
    this.volume = 1;
    this.lastEnd = 0;
  }
  ensure() {
    if (!this.ctx) {
      this.ctx = new AudioContext();
      this.gain = this.ctx.createGain();
      this.analyser = this.ctx.createAnalyser();
      this.analyser.fftSize = 1024;
      this.local = this.ctx.createGain(); // 要不要在当前输出设备上出声，由 output.js 控制
      this.analyser.connect(this.gain).connect(this.local).connect(this.ctx.destination); // 电平表取音量调节之前的信号
      this.gain.gain.value = this.volume;
      this.onCreate?.();
    }
    if (this.ctx.state === "suspended") this.ctx.resume();
  }
  setVolume(v) { this.volume = v; if (this.gain) this.gain.gain.value = v; }
  get playing() { return !!this.cur; }
  get audible() { return !!this.cur || performance.now() - this.lastEnd < 350; }
  add(job) { this.queue.push(job); this.pump(); }
  remove(job) {
    this.queue = this.queue.filter(j => j !== job);
    if (this.cur?.job === job) this.halt();
    this.pump();
  }
  halt() {
    const c = this.cur;
    if (!c) return;
    this.cur = null;
    c.sources.forEach(s => { s.onended = null; s.stop(); });
    this.lastEnd = performance.now();
    c.job.onEnd(true);
  }
  stopCurrent() { this.halt(); this.pump(); }
  pump() {
    if (!this.cur) {
      while (this.queue.length && this.queue[0].dead()) this.queue.shift();
      const job = this.queue[0];
      if (!job || !job.ready() || !job.chunks.length || !this.canStart()) return;
      this.ensure();
      this.queue.shift();
      this.cur = { job, next: 0, at: this.ctx.currentTime + (job.joined ? 0.02 : 0.08), sources: [] }; // 同一句的后续段尽量紧接着播
      job.onStart();
    }
    const c = this.cur;
    while (c.next < c.job.chunks.length) {
      const pcm = c.job.chunks[c.next++];
      const buf = this.ctx.createBuffer(1, pcm.length, c.job.sr);
      const data = buf.getChannelData(0);
      for (let i = 0; i < pcm.length; i++) data[i] = pcm[i] / 32768;
      const src = this.ctx.createBufferSource();
      src.buffer = buf;
      src.connect(this.analyser);
      c.at = Math.max(c.at, this.ctx.currentTime + 0.02);
      src.start(c.at);
      c.at += buf.duration;
      src.onended = () => this.pump();
      c.sources.push(src);
    }
    if (c.job.complete() && this.ctx.currentTime >= c.at - 0.01) {
      this.cur = null;
      this.lastEnd = performance.now();
      c.job.onEnd(false);
      this.pump();
    }
  }
}
