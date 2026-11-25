import { $, $$, toast } from "./common.js";
import { initLive, engine, escapeAction } from "./live.js";
import { initTune, leaveTune, analyze, addCard } from "./tune.js";

function tab(name) {
  if (name === "tune") {
    const was = $("#power").classList.contains("live");
    engine.stop(); // 精调页要试听和录音，继续监听会把试听的声音录进去
    if (was) toast("已停止实时监听");
  } else leaveTune();
  $$(".view").forEach(v => (v.hidden = v.id !== "view-" + name));
  $$("nav button").forEach(b => b.setAttribute("aria-selected", b.dataset.tab === name));
  history.replaceState(null, "", name === "tune" ? "#tune" : location.pathname);
}

$$("nav button").forEach(b => (b.onclick = () => tab(b.dataset.tab)));
initTune();
initLive().catch(e => toast("连接服务失败：" + e.message, true));
if (location.hash === "#tune") tab("tune");
addEventListener("hashchange", () => tab(location.hash === "#tune" ? "tune" : "live"));
window.voxrelay = { engine, escapeAction, analyze, addCard }; // 调试用
