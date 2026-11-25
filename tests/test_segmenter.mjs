// 断句器测试：用合成信号喂给 web/audio.js 的 Segmenter。运行: node tests/test_segmenter.mjs
import assert from "node:assert/strict";
import { Segmenter, FRAME } from "../web/audio.js";

function run(parts, opts = {}) {
  let now = 0;
  globalThis.performance.now = () => now; // 用音频时间代替墙钟
  const out = [];
  let discarded = 0;
  const o = { mode: "auto", sensitivity: 60, silenceMs: 500, chunking: true, pauseMs: 200, maxChunk: 4, ...opts };
  const seg = new Segmenter(() => o, { start() {}, discard() { discarded++; }, end(f, info) { out.push({ secs: f.length * 0.03, ...info }); } });
  for (const [kind, secs] of parts) {
    for (let i = 0; i < Math.round(secs / 0.03); i++) {
      const frame = new Float32Array(FRAME);
      const amp = kind === "voice" ? 0.2 : 0.0005;
      for (let k = 0; k < FRAME; k++) frame[k] = amp * Math.sin(k / 3);
      now += 30;
      seg.push(frame);
    }
  }
  return { out, discarded };
}

// 一直不停顿：按上限强切；说完后的尾段在短停顿处发出
let r = run([["quiet", 1], ["voice", 10], ["quiet", 1]]);
assert.ok(r.out.length >= 3);
assert.ok(r.out.slice(0, -1).every(s => !s.final && s.forced && s.secs <= 4.01));
assert.equal(new Set(r.out.map(s => s.uid)).size, 1);
assert.deepEqual(r.out.map(s => s.part), r.out.map((_, i) => i));

// 短停顿处切段。已知行为：尾段语音超过 1.5 秒时也是在短停顿处发出，不带“句末”标记
r = run([["quiet", 1], ["voice", 2], ["quiet", 0.27], ["voice", 2], ["quiet", 1]]);
assert.deepEqual(r.out.map(s => [s.forced, s.final]), [[false, false], [false, false]]);

// 短句：等到说完判定，带“句末”标记
r = run([["quiet", 1], ["voice", 0.9], ["quiet", 1]]);
assert.deepEqual(r.out.map(s => s.final), [true]);

// 关掉分段：整句一次发出
r = run([["quiet", 1], ["voice", 2], ["quiet", 0.27], ["voice", 2], ["quiet", 1]], { chunking: false });
assert.deepEqual(r.out.map(s => s.final), [true]);

// 很短的杂音丢弃
r = run([["quiet", 1], ["voice", 0.12], ["quiet", 1]]);
assert.deepEqual([r.out.length, r.discarded], [0, 1]);

console.log("通过：强切、停顿切段、关闭分段、丢弃杂音");
