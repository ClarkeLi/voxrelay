export const $ = s => document.querySelector(s);
export const $$ = s => [...document.querySelectorAll(s)];
export const esc = s => String(s).replace(/[&<>"']/g, c => `&#${c.charCodeAt(0)};`);

export async function post(url, body, raw) {
  const r = await fetch(url, { method: "POST", body: raw ? body : JSON.stringify(body) });
  const j = await r.json();
  if (!r.ok) throw new Error(j.error || "请求失败");
  return j;
}

let toastTimer = 0;
export function toast(msg, bad) {
  const t = $("#toast");
  t.textContent = msg;
  t.className = "show" + (bad ? " bad" : "");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => (t.className = ""), bad ? 6000 : 2600);
}
