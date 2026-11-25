// 声音送到哪里：本机扬声器 / 本机虚拟声卡 / 内网另一台 Mac 的声卡
import { $, esc, toast } from "./common.js";

const VIRTUAL = /blackhole|loopback|virtual|vb-?cable|soundflower|虚拟/i;
const BACKLOG = 96000; // WebSocket 积压超过约 1 秒就丢掉新数据

export function createOutput({ player, settings, save, changed }) {
  let relayPort = 0, ws = null, tap = null, worklet = null, monitor = null, sinkError = "", seq = 0, linkSeq = 0, scanning = false;
  let link = { state: "idle", text: "" }, sinks = [];

  const remote = () => settings.out === "remote";
  // 声音只进虚拟声卡或另一台机器，本机麦克风录不到
  const away = () => remote() || (settings.out === "sink" && VIRTUAL.test(settings.sinkLabel));
  const quiet = () => settings.headphones || (away() && !settings.monitor);
  function blocked() {
    if (settings.out === "sink" && sinkError) return "输出声卡不可用";
    if (remote() && link.state !== "ready") return link.state === "connecting" ? "远程连接中" : "远程未连接";
    return "";
  }

  function render() {
    const out = settings.out;
    $("#out").value = out;
    $("#sinkBox").hidden = out !== "sink";
    $("#remoteBox").hidden = out !== "remote";
    $("#monitorRow").hidden = out === "local";
    const noEcho = away() && !settings.monitor;
    $("#headphonesRow").hidden = noEcho;
    $("#quietNote").hidden = !noEcho;
    $("#remoteScan").disabled = scanning;
    let text = "", cls = "";
    if (out === "sink") {
      if (sinkError) [text, cls] = [sinkError, "bad"];
      else if (!sinks.length) [text, cls] = ["开始监听（允许麦克风）后才能列出声卡。", "warn"];
      else if (settings.sink && !away()) [text, cls] = ["这个设备不像虚拟声卡，按外放处理：说完才播。", "warn"];
      else if (!sinks.some(d => VIRTUAL.test(d.label))) [text, cls] = ["本机还没有虚拟声卡，可安装 BlackHole 2ch。", "warn"];
      else if (settings.sink) [text, cls] = [`会议软件的麦克风请选 ${settings.sinkLabel}`, "ok"];
    } else if (out === "remote") {
      text = link.text || (player.ctx ? "未连接" : "点“开始监听”或“重新连接”后连接");
      cls = { ready: "ok", connecting: "work", error: "bad" }[link.state] || "";
    }
    const p = $("#outStatus");
    p.hidden = !text; p.textContent = text; p.className = "link " + cls;
    changed();
  }
  function setLink(state, text) { link = { state, text }; render(); }

  // ---------- 本机 ----------
  function stopMonitor() {
    if (!monitor) return;
    monitor.audio.pause();
    player.gain.disconnect(monitor.node);
    monitor = null;
  }
  function startMonitor() { // 另开一路播到系统默认设备
    const node = player.ctx.createMediaStreamDestination();
    player.gain.connect(node);
    const audio = new Audio();
    audio.srcObject = node.stream;
    audio.play().catch(e => toast("本机监听打不开：" + e.message, true));
    monitor = { node, audio };
  }
  async function apply() {
    const ctx = player.ctx;
    if (!ctx) return render();
    const my = ++seq, out = settings.out;
    sinkError = "";
    stopMonitor();
    if (out !== "remote") disconnect();
    player.local.gain.value = 0; // 切换完成前不出声，避免放错地方
    try {
      const sink = out === "sink" ? settings.sink : "";
      if (out === "sink" && !sink) throw new Error("请选择一个声卡");
      if (ctx.setSinkId) await ctx.setSinkId(sink);
      else if (sink) throw new Error("这个浏览器不能切换输出设备，请用新版 Chrome");
    } catch (e) {
      if (my === seq) { sinkError = e.name === "NotFoundError" ? "之前选的声卡不见了，请重新选择" : e.message; render(); }
      return;
    }
    if (my !== seq) return;
    player.local.gain.value = out === "remote" ? +settings.monitor : 1;
    if (out === "sink" && settings.monitor) startMonitor();
    if (out === "remote" && link.state === "idle") connect();
    render();
  }

  async function list() {
    if (!navigator.mediaDevices?.enumerateDevices) return;
    sinks = (await navigator.mediaDevices.enumerateDevices()).filter(d => d.kind === "audiooutput" && d.deviceId && !["default", "communications"].includes(d.deviceId));
    let lost = false;
    if (sinks.length && settings.sink && !sinks.some(d => d.deviceId === settings.sink)) {
      const same = sinks.find(d => d.label && d.label === settings.sinkLabel); // 设备编号会变，按名字找回
      if (same) { settings.sink = same.deviceId; save(); } else lost = true;
    }
    $("#sink").innerHTML = `<option value="">请选择…</option>` + sinks.map((d, i) => `<option value="${esc(d.deviceId)}">${esc(d.label || "输出设备 " + (i + 1))}</option>`).join("");
    $("#sink").value = lost ? "" : settings.sink;
    if (settings.out === "sink" && player.ctx) apply(); else render();
  }

  // ---------- 内网另一台机器 ----------
  const open = () => new WebSocket(`ws://${location.hostname}:${relayPort}`);
  function disconnect() {
    linkSeq++;
    const s = ws; ws = null;
    s?.close();
    if (link.state !== "idle") setLink("idle", "");
  }
  function fail(message) {
    const was = link.state === "ready";
    const s = ws; ws = null;
    s?.close();
    setLink("error", message);
    if (was) toast("远程声卡断开：" + message, true);
  }
  async function ensureTap() {
    const ctx = player.ctx;
    await (worklet ||= ctx.audioWorklet.addModule("/static/pcm-worklet.js").catch(e => { worklet = null; throw e; }));
    if (tap) return;
    tap = new AudioWorkletNode(ctx, "pcm-tap", { outputChannelCount: [1] });
    const mute = ctx.createGain();
    mute.gain.value = 0; // 接到输出只是为了让节点持续运转
    player.gain.connect(tap).connect(mute).connect(ctx.destination);
    tap.port.onmessage = e => { if (link.state === "ready" && ws?.readyState === 1 && ws.bufferedAmount < BACKLOG) ws.send(e.data); };
  }
  async function connect() {
    disconnect();
    if (!relayPort) return setLink("error", "服务端没有开启远程通道，请重启服务");
    if (!settings.remoteTarget) return setLink("error", "请填写对方的 SSH 地址");
    const mine = ++linkSeq;
    setLink("connecting", "连接中…第一次会把播放程序拷过去，稍等");
    try { await ensureTap(); } catch (e) { return setLink("error", "音频处理模块加载失败：" + e.message); }
    if (mine !== linkSeq) return;
    const s = (ws = open());
    let device = "";
    s.onopen = () => s.send(JSON.stringify({ type: "open", target: settings.remoteTarget, device: settings.remoteDevice, rate: Math.round(player.ctx.sampleRate) }));
    s.onmessage = e => {
      if (ws !== s) return;
      const m = JSON.parse(e.data);
      if (m.type === "ready") { device = m.device; setLink("ready", `已连接，正在写入 ${device}`); }
      else if (m.type === "stat") setLink("ready", `已连接，正在写入 ${device} · 缓冲 ${m.buffer_ms} ms${m.underruns ? ` · 断续 ${m.underruns} 次` : ""}`);
      else if (m.type === "error") fail(m.message);
    };
    s.onclose = () => { if (ws === s) fail("连接断开"); };
  }
  function showRemoteDevices(found) {
    const outs = found.filter(d => d.outputs > 0).sort((a, b) => b.virtual - a.virtual);
    if (!outs.some(d => d.name === settings.remoteDevice)) {
      const v = outs.find(d => VIRTUAL.test(d.name));
      if (v) { settings.remoteDevice = v.name; save(); }
    }
    fillRemote(outs.map(d => d.name));
    if (!outs.some(d => VIRTUAL.test(d.name))) toast("对方还没有虚拟声卡，请先在那台电脑上安装 BlackHole 2ch", true);
  }
  function fillRemote(names) {
    const all = settings.remoteDevice && !names.includes(settings.remoteDevice) ? [settings.remoteDevice, ...names] : names;
    $("#remoteDevice").innerHTML = all.map(n => `<option value="${esc(n)}">${esc(n)}</option>`).join("");
    $("#remoteDevice").value = settings.remoteDevice;
  }
  function scan() {
    if (!relayPort || scanning) return;
    scanning = true; $("#remoteScan").textContent = "读取中…"; render();
    const s = open();
    const done = () => { scanning = false; $("#remoteScan").textContent = "读取声卡列表"; render(); };
    let answered = false;
    s.onopen = () => s.send(JSON.stringify({ type: "devices", target: settings.remoteTarget }));
    s.onmessage = e => {
      const m = JSON.parse(e.data);
      if (m.type === "log") return toast(m.message);
      answered = true;
      if (m.type === "devices") { showRemoteDevices(m.devices); toast(`读到 ${m.devices.length} 个声卡设备`); }
      else toast(m.message, true);
    };
    s.onclose = () => { if (!answered) toast("读取失败：远程通道断开", true); done(); };
  }

  function testTone() {
    player.ensure();
    if (remote() && link.state !== "ready") return toast("远程还没连上，连上后再试", true);
    if (sinkError) return toast(sinkError, true);
    const ctx = player.ctx, t = ctx.currentTime + 0.1, g = ctx.createGain(), o = ctx.createOscillator();
    o.frequency.value = 660;
    g.gain.setValueAtTime(0, t); g.gain.linearRampToValueAtTime(0.2, t + 0.02); g.gain.setValueAtTime(0.2, t + 0.5); g.gain.linearRampToValueAtTime(0, t + 0.6);
    o.connect(g).connect(player.analyser);
    o.start(t); o.stop(t + 0.62);
    player.lastEnd = performance.now() + 600; // 外放时别让麦克风把提示音录进去
    toast("已发送测试音");
  }

  // ---------- 控件 ----------
  player.onCreate = () => apply();
  $("#out").onchange = e => { settings.out = e.target.value; save(); player.ctx ? apply() : player.ensure(); };
  $("#sink").onchange = e => {
    settings.sink = e.target.value;
    settings.sinkLabel = sinks.find(d => d.deviceId === settings.sink)?.label || "";
    save(); player.ctx ? apply() : player.ensure();
  };
  $("#monitor").checked = settings.monitor;
  $("#monitor").onchange = e => { settings.monitor = e.target.checked; save(); player.ctx ? apply() : render(); };
  const reconnect = () => { if (!player.ctx) player.ensure(); else { player.ensure(); connect(); } };
  $("#remoteTarget").onchange = e => { settings.remoteTarget = e.target.value.trim(); e.target.value = settings.remoteTarget; save(); if (player.ctx && remote()) connect(); };
  $("#remoteDevice").onchange = e => { settings.remoteDevice = e.target.value; save(); if (player.ctx && remote()) connect(); };
  $("#remoteScan").onclick = scan;
  $("#remoteConnect").onclick = reconnect;
  $("#testTone").onclick = testTone;

  return {
    quiet, blocked, render, list, apply,
    get link() { return link; },
    resume() { if (remote() && link.state === "error") connect(); }, // 开始监听时顺便重连
    configure(cfg) {
      relayPort = cfg.relay_port || 0;
      settings.remoteTarget ||= cfg.remote?.target || "";
      settings.remoteDevice ||= cfg.remote?.device || "";
      $("#remoteTarget").value = settings.remoteTarget;
      fillRemote([]);
      render();
    },
  };
}
