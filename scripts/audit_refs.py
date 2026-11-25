#!/usr/bin/env python
"""参考录音稳定性体检：对每条 3–10 秒的参考，用「带参考文本」模式合成 3 个不同句子，语音识别回读算错字率(CER)。
CER 均值 ≤ 0.12 且最差句 ≤ 0.25 视为稳定。实时页只列出稳定的参考，并按成绩排序。
用法: 先启动服务（./start.sh），再另开终端运行 python scripts/audit_refs.py。参考多时耗时较长，可中断后续跑。
"""
import difflib, json, os, re, sys, time, urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from voxrelay import config  # noqa: E402

CFG = config.server()
H = f"http://127.0.0.1:{CFG['port']}"
DATA = CFG["data_dir"]
SENTS = ["我最近在学做饭，虽然做得不太好吃，但是越来越有意思了。", "明天下午三点半开会，记得提前十分钟到。", "你吃饭了吗？要不要我给你带点什么回来？"]
norm = lambda s: re.sub(r"[^一-鿿A-Za-z0-9]", "", s)


def post(path, body, raw=False):
    req = urllib.request.Request(H + path, data=body if raw else json.dumps(body).encode(), method="POST")
    return json.load(urllib.request.urlopen(req, timeout=300))


def main():
    with open(os.path.join(DATA, "index.json"), encoding="utf-8") as f:
        items = json.load(f)["items"]
    log = os.path.join(DATA, "audit.jsonl")
    done = set()
    if os.path.exists(log):
        done = {json.loads(l)["name"] for l in open(log, encoding="utf-8")}
    t0 = time.time()
    for n, it in enumerate(items):
        if it["name"] in done:
            continue
        cers = []
        for s in SENTS:
            try:
                r = post("/api/synth", {"text": s, "ref": it["path"], "use_prompt": True, "seed": 42})
                a = post("/api/analyze", urllib.request.urlopen(H + r["url"]).read(), raw=True)
                cers.append(1 - difflib.SequenceMatcher(None, norm(s), norm(a["text"])).ratio())
            except Exception as e:  # 合成/识别出错的参考直接记为不稳定
                print("  出错:", it["name"], e, flush=True)
                cers.append(1.0)
        row = {"name": it["name"], "path": it["path"], "cers": cers, "mean": sum(cers) / len(cers), "max": max(cers)}
        with open(log, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
        print(f"[{n+1}/{len(items)}] {it['name']} mean={row['mean']:.3f} max={row['max']:.3f}  {(time.time()-t0)/60:.1f}min", flush=True)
    rows = [json.loads(l) for l in open(log, encoding="utf-8")]
    stable = [r["name"] for r in rows if r["mean"] <= 0.12 and r["max"] <= 0.25]
    with open(os.path.join(DATA, "stable.json"), "w", encoding="utf-8") as f:
        json.dump(stable, f)
    print(f"稳定 {len(stable)}/{len(rows)}")


if __name__ == "__main__":
    main()
