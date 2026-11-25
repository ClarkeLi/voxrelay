// 精调页：按语气匹配参考、单条合成、抽卡
import { $, $$, esc, post, toast } from "./common.js";

let rec = null, timer = 0, A = null, chosen = 0;
let cards = [], rollKey = "", rollGen = 0, rollStop = false;
const LBL = { rate: "语速（字/秒）", f0_std: "音高起伏（半音）", f0_range: "音高跨度（半音）", tail: "句尾升降（半音）", db_std: "力度变化（dB）", pause: "停顿比例", dur: "时长（秒）" };

function setStatus(m, bad) { const s = $("#t-status"); s.className = bad ? "hint err" : "hint"; s.textContent = m || ""; }
function showMine(b) { const a = $("#t-mine"); a.src = URL.createObjectURL(b); a.hidden = false; }
function busy(m) { $("#t-ana").hidden = false; $("#t-tags").innerHTML = `<span>${esc(m)}</span>`; }
const setErr = e => { $("#t-ana").hidden = false; $("#t-tags").innerHTML = `<span class="err">${esc(e.message)}</span>`; };

export async function analyze(blob) {
  busy("识别并分析中…");
  try { show(await post("/api/analyze", blob, true)); } catch (e) { setErr(e); }
}

function show(r) {
  A = r; chosen = 0; resetRolls();
  $("#t-ana").hidden = false; $("#t-text").value = r.text;
  $("#t-tags").innerHTML = r.tags.map(t => `<span>${esc(t)}</span>`).join("");
  const f = r.feats; let rows = "";
  if (r.has_audio) {
    for (const k in LBL) if (f[k] != null) rows += `<tr><td>${LBL[k]}</td><td>${(+f[k]).toFixed(2)}</td></tr>`;
  } else rows = `<tr><td>字数</td><td>${f.char_count}</td></tr>`;
  $("#t-feat").innerHTML = rows;
  renderCands();
  $("#t-usePrompt").checked = false;
  if (r.has_audio && $("#t-matchRate").disabled) $("#t-matchRate").checked = true; // 从“仅按文字”切回录音时恢复默认勾选
  $("#t-matchRate").disabled = $("#t-vc").disabled = !r.has_audio;
  if (!r.has_audio) $("#t-matchRate").checked = $("#t-vc").checked = false;
  $("#t-synth").disabled = $("#t-roll").disabled = false;
  setStatus(r.has_audio && !r.text ? "没有识别到文字，请手动输入要合成的内容。" : "", r.has_audio && !r.text);
}

function renderCands() {
  $("#t-cands").innerHTML = A.top.map((c, i) => `<div class="cand ${i === chosen ? "sel" : ""}" data-i="${i}" tabindex="0" role="radio" aria-checked="${i === chosen}">
    <div class="t">${i + 1}. ${esc(c.text)}</div>
    <div class="m">${esc(c.name)} · ${c.dur}s · 距离 ${c.score}${c.stable ? " · 稳定" : ""}${c.tags.length ? " · " + c.tags.join("、") : ""}${c.worst.length ? " · 差异主要在：" + c.worst.join("、") : ""}</div>
    <audio controls preload="none" src="/ref?path=${encodeURIComponent(c.path)}"></audio></div>`).join("");
  $$("#t-cands .cand").forEach(el => {
    const pick = () => { // 只改选中样式，不重画，正在试听的音频不中断
      chosen = +el.dataset.i;
      $$("#t-cands .cand").forEach(x => { x.classList.toggle("sel", x === el); x.setAttribute("aria-checked", x === el); });
    };
    el.onclick = e => { if (e.target.tagName !== "AUDIO") pick(); };
    el.onkeydown = e => { if (e.target === el && (e.key === "Enter" || e.key === " ")) { e.preventDefault(); pick(); } };
  });
}

const lock = on => { $("#t-synth").disabled = $("#t-roll").disabled = on; };

async function synthOne() {
  const text = $("#t-text").value.trim(); if (!text) return setStatus("请先确认要合成的文字。", true);
  const src = A;
  lock(true); $("#t-outs").innerHTML = "";
  const base = { text, seed: +$("#t-seed").value };
  const ref = src.top[chosen];
  const jobs = [{ ...base, ref: ref.path, use_prompt: $("#t-usePrompt").checked, match_rate: $("#t-matchRate").checked && src.has_audio, target_dur: src.feats.dur, label: `匹配参考（${ref.text}）` }];
  if ($("#t-vc").checked && src.has_audio) jobs.push({ ...base, ref: ref.path, mode: "vc", src_id: src.src_id, label: "直转版本（实验：你的语义 token + 目标音色）" });
  if ($("#t-cmpDefault").checked) jobs.push({ ...base, ref: "__default__", use_prompt: false, label: "默认参考（对比）" });
  try {
    for (const j of jobs) {
      setStatus("合成中：" + j.label + "…");
      const r = await post("/api/synth", j);
      const m = [`${r.dur.toFixed(1)}s`];
      if (r.speed) m.push(`语速 ×${r.speed.toFixed(2)}（自然 ${r.natural_dur}s → 目标 ${src.feats.dur.toFixed(1)}s）`);
      if (r.fallback) m.push("⚠ " + r.fallback);
      m.push(r.cached ? "缓存" : `用时 ${r.sec.toFixed(1)}s`);
      $("#t-outs").insertAdjacentHTML("beforeend", `<div class="card"><div class="t">${esc(j.label)}</div><div class="m">${esc(m.join(" · "))}</div><audio controls src="${r.url}"></audio></div>`);
      const a = $("#t-outs").lastElementChild.querySelector("audio");
      if (!$("#view-tune").hidden) a.play().catch(() => {});
    }
    setStatus("完成");
  } catch (e) { setStatus(e.message, true); }
  lock(false);
}

// ---- 抽卡 ----
function resetRolls() { rollGen++; cards = []; rollKey = ""; $("#t-rolls").innerHTML = ""; $("#t-rollHead").hidden = true; $("#t-rollStatus").textContent = ""; }
function sortCards() {
  // 合格线：CER ≤ 0.1；若整批都超过（例如数字、英文的写法和识别结果不一致），放宽到“最低值 + 0.05”
  const min = Math.min(...cards.map(c => c.r.cer)), limit = min <= 0.1 ? 0.1 : min + 0.05;
  cards.forEach(c => (c.bad = c.r.cer > limit));
  [...cards].sort((a, b) => a.bad - b.bad || b.r.score - a.r.score).forEach((c, i) => {
    c.el.style.order = i; // 只改显示顺序，不移动节点，正在播放的音频不受影响
    c.el.className = "card" + (c.bad ? " bad" : i === 0 ? " best" : "");
    c.el.querySelector(".t").textContent = `${c.bad ? "疑似读错" : i === 0 ? "推荐第 1" : "第 " + (i + 1)}（抽到的第 ${c.n} 条）`;
  });
}
export function addCard(r) {
  const el = document.createElement("div");
  const m = [`识别正确率 ${Math.max(0, 100 * (1 - r.cer)).toFixed(0)}%`, `时长 ${r.dur.toFixed(1)}s`];
  if (r.ratio != null) m.push(`时长为你的 ${r.ratio.toFixed(2)} 倍`);
  if (r.corr != null) m.push(`语调相关 ${r.corr.toFixed(2)}`);
  m.push(`参考：${esc(r.ref.text)}（${esc(r.ref.name)}）`, `种子 ${r.seed}`);
  el.innerHTML = `<div class="t"></div><div class="m">${m.join(" · ")}${r.cer > 0 ? "<br>识别结果：" + esc(r.heard) : ""}</div><audio controls preload="none" src="${r.url}"></audio>`;
  $("#t-rolls").appendChild(el); cards.push({ r, el, n: cards.length + 1 }); sortCards();
}
async function rollBatch() {
  const text = $("#t-text").value.trim(); if (!text) { $("#t-rollStatus").textContent = "请先确认要合成的文字。"; return; }
  const match = $("#t-matchRate").checked && A.has_audio, key = JSON.stringify([text, match]);
  if (key !== rollKey) { resetRolls(); rollKey = key; } // 文字或语速设置变了，旧结果不再可比
  const gen = rollGen, src = A, n = Math.max(1, Math.min(12, Math.round(+$("#t-rollN").value) || 4));
  $("#t-rollN").value = n; rollStop = false; $("#t-rollHead").hidden = false;
  lock(true); $("#t-rollStop").hidden = false;
  let done = 0, error = "";
  for (let i = 0; i < n && !rollStop && gen === rollGen; i++) {
    $("#t-rollStatus").textContent = `抽卡中 ${i + 1}/${n}…`;
    try {
      const r = await post("/api/roll", { text, feats: src.feats, cues: src.cues, src_id: src.has_audio ? src.src_id : "", match_rate: match });
      if (gen !== rollGen) break; // 期间换了录音，丢弃
      addCard(r); done++;
    } catch (e) { error = e.message; break; }
  }
  lock(false); $("#t-rollStop").hidden = true;
  if (gen === rollGen) $("#t-rollStatus").textContent = error ? `出错：${error}（本批完成 ${done} 条）` : `本批完成 ${done} 条，累计 ${cards.length} 条`;
}

export function initTune() {
  $("#t-rec").onclick = async () => {
    const b = $("#t-rec");
    if (rec) { rec.stop(); return; }
    let stream;
    try { stream = await navigator.mediaDevices.getUserMedia({ audio: true }); }
    catch (e) { return toast("无法使用麦克风：" + e.message + "（请允许浏览器麦克风权限，或改用上传录音）", true); }
    const chunks = [], t0 = Date.now();
    rec = new MediaRecorder(stream);
    rec.ondataavailable = e => chunks.push(e.data);
    rec.onstop = () => {
      stream.getTracks().forEach(t => t.stop()); clearInterval(timer); rec = null;
      b.textContent = "● 重新录音"; b.classList.remove("live"); $("#t-recTime").textContent = "";
      const blob = new Blob(chunks, { type: chunks[0]?.type || "audio/webm" }); showMine(blob); analyze(blob);
    };
    rec.start(); b.textContent = "■ 结束录音"; b.classList.add("live");
    timer = setInterval(() => ($("#t-recTime").textContent = ((Date.now() - t0) / 1000).toFixed(1) + " s"), 100);
  };
  $("#t-file").onchange = e => { const f = e.target.files[0]; if (f) { showMine(f); analyze(f); } };
  const goText = async () => {
    const t = $("#t-typed").value.trim(); if (!t) return;
    $("#t-mine").hidden = true; busy("按文字匹配参考…");
    try { show(await post("/api/analyze_text", { text: t })); } catch (e) { setErr(e); }
  };
  $("#t-goText").onclick = goText;
  $("#t-typed").onkeydown = e => { if (e.key === "Enter" && !e.isComposing && e.keyCode !== 229) goText(); };
  $("#t-synth").onclick = synthOne;
  $("#t-roll").onclick = rollBatch;
  $("#t-rollStop").onclick = () => { rollStop = true; $("#t-rollStatus").textContent = "当前这条完成后停止…"; };
}

export function leaveTune() {
  rec?.stop();
  $$("#view-tune audio").forEach(a => a.pause());
}
