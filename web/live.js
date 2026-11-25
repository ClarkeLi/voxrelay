// 实时页：听 → 写 → 合 → 播
import { Capture, Segmenter, Player, toPcm16, frameDb, FRAME, RATE } from "./audio.js";
import { $, $$, esc, post, toast } from "./common.js";
import { createOutput } from "./output.js";

const KEY = "voxrelay.live";
const DEFAULTS = { v: 2, mode: "auto", silenceMs: 500, sensitivity: 60, chunking: true, pauseMs: 200, maxChunk: 4, holdMs: 0, groupWindow: 30, ref: "", autoRef: false, followRate: true, speed: 1, randomSeed: true, seed: 42, skipFiller: true, headphones: false, volume: 1, device: "", out: "local", sink: "", sinkLabel: "", monitor: false, remoteTarget: "", remoteDevice: "" };
const old = Object.keys(localStorage).find(k => k.endsWith(".studio.live")); // 改名前版本留下的设置，沿用
const saved = JSON.parse(localStorage.getItem(KEY) || (old && localStorage.getItem(old)) || "{}");
if (saved.v !== DEFAULTS.v) { delete saved.v; delete saved.holdMs; delete saved.silenceMs; } // 默认值改为优先即时性
export const settings = { ...DEFAULTS, ...saved };
const save = () => localStorage.setItem(KEY, JSON.stringify(settings));

const items = [];       // 本次会话的句子，按说话顺序
const latencies = [];
const wave = [];        // 输入电平历史，用于画波形
let nextId = 1, listening = false, starting = false, live = null, lastDb = -80, synthBusy = false, asrChain = Promise.resolve();
// 语气组：同一句的各段、以及时间窗口内接着说的话，共用同一个参考录音、种子和语速
let group = null, groupNo = 0;
const groupOfUtterance = new Map();
const resetGroup = () => { group = null; };
function groupFor(info, secs) {
  let g = info.part > 0 ? groupOfUtterance.get(info.uid) : null;
  if (!g) {
    const begin = info.speechEnd - secs * 1000;
    if (!group || begin - group.lastEnd > settings.groupWindow * 1000) group = { n: ++groupNo, ref: "", speed: null, seed: settings.randomSeed ? 1 + Math.floor(Math.random() * 1e6) : settings.seed, lastEnd: 0 };
    g = group;
  }
  groupOfUtterance.set(info.uid, g);
  g.lastEnd = Math.max(g.lastEnd, info.speechEnd);
  return g;
}

const player = new Player(() => output.quiet() || !segmenter.active); // 合成声会被麦克风录到时，等你说完再播
const output = createOutput({ player, settings, save, changed: () => { player.pump(); renderStatus(); } });
const capture = new Capture();
const segmenter = new Segmenter(() => settings, {
  start() { live = { id: nextId, text: "", busy: false, at: 0 }; renderLive(); },
  end(frames, info) { live = null; renderLive(); addUtterance(frames, info); },
  discard() { live = null; renderLive(); },
});

export const engine = {
  items, player, segmenter, settings, output,
  openStream: deviceId => navigator.mediaDevices.getUserMedia({ audio: { deviceId: deviceId ? { exact: deviceId } : undefined, channelCount: 1, echoCancellation: true, noiseSuppression: true, autoGainControl: true } }),
  async start() {
    if (listening || starting) return;
    starting = true;
    player.ensure(); // 必须在点击事件里创建，否则浏览器不让出声
    output.resume();
    try {
      let stream;
      try { stream = await this.openStream(settings.device); }
      catch (e) {
        if (!settings.device || e.name !== "OverconstrainedError") throw e;
        settings.device = ""; save(); stream = await this.openStream(""); // 上次选的麦克风不在了
      }
      segmenter.reset();
      await capture.start(stream, onFrame);
      listening = true;
      listDevices();
    } catch (e) {
      capture.stop();
      toast("无法使用麦克风：" + e.message + "（请在浏览器里允许麦克风权限）", true);
    } finally { starting = false; renderControls(); }
  },
  stop() {
    if (!listening) return;
    segmenter.finish();
    capture.stop();
    listening = false; lastDb = -80;
    renderControls();
  },
  clear() {
    items.slice().forEach(u => { cancel(u); cancel(u); }); // 第一次停掉正在播的，第二次撤回
    items.length = 0; latencies.length = 0; resetGroup(); groupOfUtterance.clear();
    $("#feed").innerHTML = "";
    renderStatus();
  },
};

function onFrame(frame) {
  const holding = settings.mode === "ptt" && segmenter.active;
  if (!output.quiet() && player.audible && !holding) { // 外放：播放期间不拾音，避免把自己合成的声音又录进去
    segmenter.finish();
    segmenter.clearPre();
    lastDb = frameDb(frame);
    pushWave(lastDb, "mute");
  } else {
    lastDb = segmenter.push(frame);
    pushWave(lastDb, segmenter.active ? "voice" : "idle");
    if (segmenter.active) partial();
  }
  tick(); // 用音频时钟驱动倒计时：页面在后台时定时器会变慢，这里不会
}
function pushWave(db, kind) { wave.push([db, kind]); if (wave.length > 160) wave.shift(); }

async function asr(frames, query = "") {
  return post("/api/rt/asr" + query, toPcm16(frames).buffer, true);
}

async function partial() {
  const l = live, s = segmenter.seg;
  if (!l || !s || l.busy || s.frames.length < 20 || performance.now() - l.at < 700) return;
  if (synthBusy || items.some(u => u.stage === "asr" && !u.cancelled)) return; // 先让出算力给整段识别和合成
  l.busy = true;
  try {
    const r = await asr(s.frames.slice());
    if (live === l && r.text) { l.text = r.text; renderLive(); }
  } catch { /* 边说边出字失败不影响整句识别 */ }
  l.busy = false; l.at = performance.now();
}

function addUtterance(frames, info) {
  const secs = frames.length * FRAME / RATE;
  const u = {
    id: nextId++, time: new Date(), text: "", stage: "asr", cancelled: false, editing: false, released: false, holdUntil: 0,
    uid: info.uid, part: info.part, final: info.final, forced: info.forced, start: info.start, group: groupFor(info, secs),
    speechEnd: info.speechEnd, secs, synth: "none", chunks: [], sr: 32000, played: false, playing: false,
  };
  items.push(u); mount(u);
  asrChain = asrChain.then(async () => {
    if (u.cancelled) return;
    try {
      const r = await asr(frames, "?final=1" + (settings.autoRef && !u.group.ref ? "&auto_ref=1" : ""));
      if (u.cancelled) return;
      const bare = (r.text || "").replace(/[^一-鿿A-Za-z0-9]/g, "");
      if (!bare || (settings.skipFiller && /^[嗯啊呃额哦噢唔呀哎欸诶]+$/.test(bare))) {
        drop(u);
        if (bare) toast("忽略语气词：" + r.text);
        return;
      }
      // 识别模型会给每一段补句号；话还没说完的段改成逗号，合成出来语调才连得上
      const text = u.final ? r.text : u.forced ? r.text.replace(/[。，、！？,.!?]+$/, "") + "，" : r.text.replace(/[。.]+$/, "，");
      Object.assign(u, { text, rate: r.rate, autoRef: r.ref, asrSec: r.asr_sec, stage: "ready", synth: "wait", holdUntil: performance.now() + settings.holdMs, holdMs: settings.holdMs, released: !settings.holdMs });
      enqueue(u);
    } catch (e) { u.stage = "ready"; u.synth = "error"; u.error = "识别失败：" + e.message; render(u); }
    tick();
  });
  renderStatus();
}

function drop(u) {
  items.splice(items.indexOf(u), 1);
  u.el.remove();
  renderStatus();
}

// 放进播放队列。每次入队用独立的 job，重播和改字不会互相干扰。
function enqueue(u) {
  const job = {
    get chunks() { return u.chunks; }, get sr() { return u.sr; }, joined: u.part > 0,
    complete: () => u.synth === "done" || u.synth === "error",
    ready: () => u.released && !u.editing,
    dead: () => u.job !== job || u.cancelled || (u.synth === "error" && !u.chunks.length),
    onStart() {
      u.playing = true;
      if (!u.played && u.speechEnd) {
        u.latency = (performance.now() - u.speechEnd) / 1000;
        if (u.part === 0) u.fromStart = (performance.now() - u.start) / 1000;
        latencies.push(u.latency); if (latencies.length > 10) latencies.shift();
      }
      render(u);
    },
    onEnd() { u.playing = false; u.played = true; render(u); },
  };
  u.job = job;
  player.add(job);
  render(u);
  schedule();
}

// 合成一次只跑一句，按说话顺序
async function schedule() {
  if (synthBusy) return;
  const u = items.find(x => x.synth === "wait" && !x.cancelled && !x.editing);
  if (!u) return;
  synthBusy = true;
  const ctl = new AbortController(), chunks = [], t0 = performance.now();
  Object.assign(u, { synth: "running", ctl, chunks, error: "", ttfb: undefined });
  render(u);
  const g = u.group;
  g.ref ||= (settings.autoRef && u.autoRef) || settings.ref; // 组里第一段决定参考，之后沿用
  try {
    const res = await fetch("/api/rt/synth", {
      method: "POST", signal: ctl.signal,
      body: JSON.stringify({ text: u.text, ref: g.ref, speed: g.speed ?? settings.speed, follow_rate: g.speed == null && settings.followRate, rate: u.rate || 0, seed: g.seed, cont: !u.final }),
    });
    if (!res.ok) throw new Error((await res.json()).error);
    Object.assign(u, { sr: +res.headers.get("X-Sample-Rate"), usedRef: res.headers.get("X-Ref"), usedSeed: res.headers.get("X-Seed"), usedSpeed: +res.headers.get("X-Speed") });
    // 语速也按组锁定；太短的段量不准语速，等下一段
    if (g.speed == null && (!settings.followRate || u.text.replace(/[^\u4e00-\u9fffA-Za-z0-9]/g, "").length >= 5)) g.speed = u.usedSpeed;
    const reader = res.body.getReader();
    let buf = new Uint8Array(0), finished = false;
    while (!finished) {
      const { value, done } = await reader.read();
      if (done) break;
      const merged = new Uint8Array(buf.length + value.length);
      merged.set(buf); merged.set(value, buf.length); buf = merged;
      while (buf.length >= 4) {
        const n = new DataView(buf.buffer, buf.byteOffset).getUint32(0, true);
        if (n === 0) { finished = true; break; }
        if (buf.length < 4 + n) break;
        chunks.push(new Int16Array(buf.slice(4, 4 + n).buffer));
        buf = buf.subarray(4 + n);
        if (u.ttfb === undefined) { u.ttfb = (performance.now() - t0) / 1000; render(u); }
        player.pump();
      }
    }
    if (!finished) throw new Error("合成中断");
    if (u.ctl === ctl) u.synth = "done";
  } catch (e) {
    if (e.name !== "AbortError" && u.ctl === ctl) { u.synth = "error"; u.error = "合成失败：" + e.message; }
  }
  synthBusy = false;
  render(u);
  player.pump();
  schedule();
}

function resetSynth(u) { // 只丢掉合成结果，保留在播放队列里的位置
  u.ctl?.abort(); u.ctl = null;
  Object.assign(u, { synth: "none", chunks: [], ttfb: undefined, error: "" });
}

// 撤回还没播的句子；正在播的只是停掉
function cancel(u) {
  if (u.cancelled) return;
  const wasPlaying = u.playing;
  if (u.synth !== "done") { u.ctl?.abort(); u.ctl = null; u.synth = "none"; u.chunks = []; }
  if (!wasPlaying) u.cancelled = true;
  u.editing = false;
  if (u.job) { const j = u.job; player.remove(j); u.job = null; } // 先停播放（会回调 onEnd），再解除绑定
  u.playing = false;
  render(u);
}

// 立即发送（也用于撤回后重发、重试、重播）
function send(u) {
  if (!u.text) return;
  if (u.cancelled || u.synth === "error") u.speechEnd = 0; // 手动重发的句子不计入“说完到出声”
  const queued = u.job && !u.cancelled && !u.played && u.synth !== "error";
  u.cancelled = false; u.released = true; u.editing = false;
  if (queued) { render(u); schedule(); player.pump(); return; } // 已在队列里：原位放行，不打乱顺序
  if (u.job) { const j = u.job; u.job = null; player.remove(j); }
  if (u.synth !== "done" || !u.chunks.length) { resetSynth(u); u.synth = "wait"; }
  enqueue(u);
}

function startEdit(u) {
  if (u.stage !== "ready" || u.playing || u.editing) return;
  u.editing = true;
  render(u);
  const input = u.el.querySelector("input");
  input.focus(); input.select();
}
function endEdit(u, commit) {
  if (!u.editing) return;
  const text = u.el.querySelector("input").value.trim();
  u.editing = false;
  if (commit && text) {
    if (text !== u.text) {
      const queued = u.job && !u.cancelled && !u.played && u.synth !== "error";
      resetSynth(u); u.text = text; u.speechEnd = 0; u.synth = "wait";
      if (!queued) { u.played = false; u.job = null; } // 撤回过或播过的句子改字后排到队尾
    }
    send(u);
  } else {
    if (!u.released) u.holdUntil = performance.now() + settings.holdMs;
    render(u); schedule(); player.pump();
  }
}

export function escapeAction() {
  const waiting = u => !u.cancelled && !u.played && !u.playing && u.synth !== "error";
  const speaking = segmenter.seg?.uid;
  if (segmenter.cancel()) { items.filter(u => u.uid === speaking && waiting(u)).forEach(cancel); return toast("已丢弃正在说的这句"); }
  const last = [...items].reverse().find(waiting);
  if (last) { // 一句话切成的多段一起撤回
    const parts = items.filter(u => u.uid === last.uid && waiting(u));
    parts.forEach(cancel);
    return toast("已撤回：" + (parts.map(u => u.text).join("") || "识别中的句子"));
  }
  if (player.playing) { player.stopCurrent(); toast("已停止播放"); }
}

function tick() {
  const now = performance.now();
  for (const u of items) {
    if (u.stage === "ready" && !u.released && !u.editing && !u.cancelled && now >= u.holdUntil) { u.released = true; render(u); }
  }
  player.pump();
}

// ---------- 渲染 ----------
function state(u) {
  if (u.cancelled) return ["已撤回", "off"];
  if (u.stage === "asr") return ["识别中", "work"];
  if (u.editing) return ["修改中", "hold"];
  if (u.synth === "error") return ["出错", "bad"];
  if (u.playing) return ["播放中", "play"];
  if (!u.released) return ["可撤回", "hold"];
  if (u.played) return ["已播放", "ok"];
  if (!u.chunks.length) return ["合成中", "work"];
  return ["待播放", "work"];
}

function mount(u) {
  u.el = document.createElement("li");
  u.el.dataset.id = u.id;
  u.el.addEventListener("click", e => {
    const act = e.target.closest("[data-act]")?.dataset.act;
    if (act === "cancel") cancel(u);
    else if (act === "send" || act === "replay") send(u);
    else if (act === "edit") startEdit(u);
  });
  $("#feed").appendChild(u.el);
  render(u);
  scrollFeed();
}

function render(u) {
  if (!u.el.isConnected) return;
  const [label, cls] = state(u);
  renderStatus();
  if (u.editing && u.el.querySelector("input")) return; // 别打断正在输入的内容
  const meta = [`${u.secs.toFixed(1)}s 语音`];
  if (u.asrSec != null) meta.push(`识别 ${u.asrSec.toFixed(2)}s`);
  if (u.ttfb != null) meta.push(`首包 ${u.ttfb.toFixed(2)}s`);
  if (u.latency != null) meta.push(`段尾到出声 <b>${u.latency.toFixed(1)}s</b>`);
  if (u.fromStart != null && !u.final) meta.push(`开口到出声 ${u.fromStart.toFixed(1)}s`);
  if (u.usedSpeed && Math.abs(u.usedSpeed - 1) > 0.02) meta.push(`语速 ×${u.usedSpeed.toFixed(2)}`);
  if (u.usedRef) meta.push(`<span title="同一语气组共用参考、种子和语速。参考 ${esc(u.usedRef)} · 种子 ${esc(u.usedSeed)}">组${u.group.n} · ${esc(u.usedRef.replace(".wav", ""))}</span>`);
  const acts = [];
  if (u.cancelled) { if (u.text) acts.push(`<button data-act="send" title="发送">↺ 发送</button>`); }
  else if (u.synth === "error") { if (u.text) acts.push(`<button data-act="send">重试</button>`); }
  else if (!u.released && u.stage === "ready") acts.push(`<button data-act="send" title="不等倒计时">立即发送</button>`, `<button data-act="cancel" class="danger" title="撤回 (Esc)">撤回</button>`);
  else if (u.played && !u.playing) acts.push(`<button data-act="replay" title="再播一次">▶ 重播</button>`);
  else acts.push(`<button data-act="cancel" class="danger">${u.playing ? "停止" : "撤回"}</button>`);
  const body = u.editing
    ? `<input value="${esc(u.text)}" aria-label="修改文字"><div class="tip">回车发送 · Esc 放弃修改</div>`
    : `<p ${u.stage === "ready" && u.text && !u.playing ? 'data-act="edit" title="点击修改文字"' : ""}>${u.text ? esc(u.text) : u.cancelled || u.error ? '<span class="shimmer still">（没有文字）</span>' : '<span class="shimmer">正在识别…</span>'}</p>`;
  u.el.className = "utt " + cls + (u.part ? " join" : "");
  u.el.innerHTML = `<time>${u.time.toTimeString().slice(0, 8)}</time>
    <div class="body">${body}<div class="meta">${u.error ? `<em>${esc(u.error)}</em> · ` : ""}${meta.join(" · ")}</div></div>
    <div class="side"><span class="chip ${cls}">${label}</span><div class="acts">${acts.join("")}</div></div>
    <i class="bar"></i>`;
  const input = u.el.querySelector("input");
  if (input) input.onkeydown = e => {
    if (e.isComposing || e.keyCode === 229) return; // 输入法选字时的回车不算
    if (e.key === "Enter") { e.preventDefault(); endEdit(u, true); }
    if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); endEdit(u, false); }
  };
}

function renderLive() {
  const el = $("#hearing");
  el.hidden = !live;
  if (live) el.querySelector("p").textContent = live.text || "正在听…";
  renderStatus();
  scrollFeed();
}
function scrollFeed() { const s = $("#feedScroll"); s.scrollTop = s.scrollHeight; }

function renderStatus() {
  $("#empty").hidden = !!(items.length || live);
  const active = {
    hear: segmenter.active, asr: items.some(u => u.stage === "asr" && !u.cancelled),
    tts: items.some(u => u.synth === "running"), play: player.playing,
  };
  for (const k in active) $(`#stage-${k}`).classList.toggle("on", active[k]);
  const muted = listening && !output.quiet() && player.audible;
  const [text, cls] = player.playing ? [muted ? "播放中 · 暂停拾音" : "播放中", "play"] : segmenter.active ? ["正在说话", "voice"] : listening ? [settings.mode === "ptt" ? "按住空格说话" : "监听中", "on"] : ["待机", ""];
  const blocked = output.blocked(); // 声音到不了目的地时，这一条优先显示
  const pill = $("#pill"); if (pill.textContent !== (blocked || text)) pill.textContent = blocked || text; pill.className = "pill " + (blocked ? "bad" : cls);
  const sorted = [...latencies].sort((a, b) => a - b);
  $("#latency").textContent = sorted.length ? sorted[Math.floor(sorted.length / 2)].toFixed(1) + "s" : "—";
}

function renderControls() {
  const b = $("#power");
  b.classList.toggle("live", listening);
  b.textContent = listening ? "■ 停止监听" : "● 开始监听";
  b.disabled = starting;
  $$("#mode button").forEach(x => x.setAttribute("aria-pressed", x.dataset.v === settings.mode));
  $("#ptt").hidden = settings.mode !== "ptt";
  $("#ptt").disabled = !listening;
  $("#silenceRow").hidden = settings.mode !== "auto";
  $("#pauseRow").hidden = $("#maxRow").hidden = !settings.chunking;
  renderStatus();
}

function meter(el, db) { el.style.setProperty("--v", Math.max(0, Math.min(1, (db + 70) / 70))); }
const outBuf = new Float32Array(1024);
function draw() {
  requestAnimationFrame(draw);
  meter($("#inMeter"), listening ? lastDb : -80);
  $("#inMeter").style.setProperty("--t", Math.max(0, Math.min(1, (segmenter.threshold + 70) / 70)));
  $("#inDb").textContent = listening ? lastDb.toFixed(0) + " dB" : "—";
  let out = -80;
  const cv = $("#outWave"), g = fit(cv);
  g.clearRect(0, 0, cv.width, cv.height);
  g.strokeStyle = css("--play"); g.lineWidth = devicePixelRatio; g.beginPath();
  if (player.analyser && player.playing) { player.analyser.getFloatTimeDomainData(outBuf); out = frameDb(outBuf); }
  else outBuf.fill(0);
  for (let x = 0; x < cv.width; x++) {
    const y = cv.height / 2 - outBuf[Math.floor(x / cv.width * outBuf.length)] * cv.height * 0.9;
    x ? g.lineTo(x, y) : g.moveTo(x, y);
  }
  g.stroke();
  meter($("#outMeter"), out);
  const iv = $("#inWave"), h = fit(iv), w = iv.width / 160;
  h.clearRect(0, 0, iv.width, iv.height);
  const colors = { idle: css("--faint"), voice: css("--voice"), mute: css("--line") };
  wave.forEach(([db, kind], i) => {
    const a = Math.max(devicePixelRatio, Math.pow(Math.max(0, (db + 70) / 70), 1.6) * iv.height);
    h.fillStyle = colors[kind];
    h.fillRect((160 - wave.length + i) * w, (iv.height - a) / 2, Math.max(1, w - 1), a);
  });
  const now = performance.now();
  for (const u of items) if (!u.released && !u.cancelled && u.stage === "ready" && u.holdMs) {
    u.el.querySelector(".bar")?.style.setProperty("--p", u.editing ? 1 : Math.max(0, (u.holdUntil - now) / u.holdMs));
  }
  renderStatus();
}
function fit(c) {
  const w = Math.round(c.clientWidth * devicePixelRatio), h = Math.round(c.clientHeight * devicePixelRatio);
  if (c.width !== w || c.height !== h) { c.width = w; c.height = h; }
  return c.getContext("2d");
}
const cssCache = {};
const css = n => cssCache[n] ??= getComputedStyle(document.documentElement).getPropertyValue(n).trim();

async function listDevices() {
  if (!navigator.mediaDevices?.enumerateDevices) return;
  const list = (await navigator.mediaDevices.enumerateDevices()).filter(d => d.kind === "audioinput" && d.deviceId && d.deviceId !== "default");
  $("#device").innerHTML = `<option value="">系统默认麦克风</option>` + list.map((d, i) => `<option value="${esc(d.deviceId)}">${esc(d.label || "麦克风 " + (i + 1))}</option>`).join("");
  $("#device").value = list.some(d => d.deviceId === settings.device) ? settings.device : "";
  output.list();
}

export async function initLive() {
  // 控件与设置的双向绑定
  const bind = (id, key, fmt, after) => {
    const el = $("#" + id), check = el.type === "checkbox", num = el.type === "range" || el.type === "number";
    const show = () => { if (fmt) $(`#${id}Val`).textContent = fmt(settings[key]); };
    el[check ? "checked" : "value"] = settings[key]; show();
    el.addEventListener(el.type === "range" ? "input" : "change", () => {
      settings[key] = check ? el.checked : num ? +el.value : el.value;
      save(); show(); after?.();
    });
  };
  bind("silence", "silenceMs", v => (v / 1000).toFixed(1) + " s");
  bind("sens", "sensitivity", v => v);
  bind("hold", "holdMs", v => (v ? (v / 1000).toFixed(1) + " s" : "关"));
  bind("chunking", "chunking", null, () => renderControls());
  bind("pause", "pauseMs", v => v + " ms");
  bind("maxChunk", "maxChunk", v => v + " s");
  bind("groupWindow", "groupWindow", v => v + " s");
  bind("speed", "speed", v => "×" + v.toFixed(2), resetGroup);
  bind("volume", "volume", v => Math.round(v * 100) + "%", () => player.setVolume(settings.volume));
  bind("followRate", "followRate", null, resetGroup);
  bind("skipFiller", "skipFiller");
  bind("headphones", "headphones", null, () => { player.pump(); output.render(); });
  bind("autoRef", "autoRef", null, () => { $("#ref").disabled = settings.autoRef; resetGroup(); });
  bind("randomSeed", "randomSeed", null, () => { $("#seed").disabled = settings.randomSeed; resetGroup(); });
  bind("seed", "seed", null, resetGroup);
  player.setVolume(settings.volume);
  $("#seed").disabled = settings.randomSeed;
  $("#ref").disabled = settings.autoRef;

  $("#power").onclick = () => (listening ? engine.stop() : engine.start());
  $("#stopPlay").onclick = () => player.stopCurrent();
  $("#clear").onclick = () => engine.clear();
  $$("#mode button").forEach(b => (b.onclick = () => { segmenter.finish(); settings.mode = b.dataset.v; save(); renderControls(); b.blur(); }));
  $("#device").onchange = async e => { settings.device = e.target.value; save(); if (listening) { engine.stop(); await engine.start(); } };

  const talk = on => {
    if (!listening || settings.mode !== "ptt") return;
    if (on) { segmenter.press(); if (!output.quiet()) player.stopCurrent(); } // 外放时按下即打断播放
    else segmenter.release();
    $("#ptt").classList.toggle("down", on);
  };
  const ptt = $("#ptt");
  ptt.onpointerdown = e => { ptt.setPointerCapture(e.pointerId); talk(true); };
  ptt.onpointerup = ptt.onpointercancel = () => talk(false);
  const typing = el => el.matches?.("input:not([type=checkbox]):not([type=range]), textarea, select");
  addEventListener("keydown", e => {
    if ($("#view-live").hidden || e.isComposing) return;
    if (e.code === "Space" && settings.mode === "ptt" && !typing(e.target)) { e.preventDefault(); if (!e.repeat) talk(true); }
    if (e.key === "Escape" && !e.defaultPrevented) { e.preventDefault(); escapeAction(); }
  });
  addEventListener("keyup", e => { if (e.code === "Space" && settings.mode === "ptt" && !typing(e.target)) { e.preventDefault(); talk(false); } });
  addEventListener("blur", () => talk(false));
  navigator.mediaDevices?.addEventListener?.("devicechange", listDevices);

  setInterval(tick, 100);
  renderControls(); listDevices(); draw();

  const cfg = await (await fetch("/api/config")).json();
  output.configure(cfg);
  $("#t-cmpDefault").closest("label").hidden = !cfg.default_ref;
  $("#ref").innerHTML = cfg.refs.map(r => `<option value="${esc(r.name)}">${esc(r.name.replace(".wav", ""))} · ${esc(r.text)}</option>`).join("");
  if (!cfg.refs.some(r => r.name === settings.ref)) settings.ref = cfg.refs[0]?.name || "";
  $("#ref").value = settings.ref;
  const preview = () => { const r = cfg.refs.find(x => x.name === settings.ref); if (r) $("#refAudio").src = "/ref?path=" + encodeURIComponent(r.path); };
  $("#ref").onchange = e => { settings.ref = e.target.value; save(); preview(); resetGroup(); };
  preview();
  $("#model").textContent = `${cfg.weights.SoVITS.replace(".pth", "")} · ${cfg.weights.GPT.replace(".ckpt", "")}`;
}
