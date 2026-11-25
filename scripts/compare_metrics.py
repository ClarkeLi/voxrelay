#!/usr/bin/env python
"""对比几套变声系统在同一批输入上的客观指标。

用法（与服务相同的 Python 环境，另需 openai-whisper、scikit-learn、opencc）:
  python scripts/compare_metrics.py --source 输入目录 --whisper medium.pt路径 \\
      --system "名称A=输出目录A" --system "名称B=输出目录B" --out result.json
各输出目录里的 wav 与输入目录同名一一对应。目标音色取 config.json 里的参考列表（80% 建基准，20% 作真人对照）。

指标:
  可懂度    Whisper 分别识别输入和输出，以输入的识别结果为参照算字错率 CER（越低越好）。识别器与被测系统无关。
            单条超过 1 按 1 计（Whisper 偶尔陷入重复输出，会让个别条目高达好几倍）。
  音色相似  ① ERes2NetV2 说话人向量与目标中心的余弦。注意 GPT-SoVITS v2Pro 合成时用的就是这个模型，对它有利。
            ② MFCC 高斯混合模型的平均对数似然，与任何一方都无关。两项都是越高越像。
  语调保留  按频谱做时间对齐（DTW）后，两边都有声的帧上对数音高的相关系数。“错配”是把输出和另一条输入配对算出的随机水平。
  时长保留  输出语音时长 / 输入语音时长。
输出文件已存在时复用其中的识别结果，其余指标重新计算。
"""
import argparse, glob, json, os, re, sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from voxrelay import config  # noqa: E402

CFG = config.server()
P = argparse.ArgumentParser()
P.add_argument("--source", required=True)
P.add_argument("--system", action="append", required=True, help="名称=目录，可重复")
P.add_argument("--whisper", required=True, help="Whisper 模型文件或名称")
P.add_argument("--out", required=True)
A = P.parse_args()
A.source, A.out = os.path.abspath(A.source), os.path.abspath(A.out)
SYSTEMS = {k: os.path.abspath(v) for k, v in (s.split("=", 1) for s in A.system)}
GSV = CFG["gpt_sovits_dir"]
os.chdir(GSV)
sys.path[:0] = [GSV, os.path.join(GSV, "GPT_SoVITS"), os.path.join(GSV, "GPT_SoVITS", "eres2net")]

import librosa  # noqa: E402
import numpy as np  # noqa: E402
import opencc  # noqa: E402
import torch  # noqa: E402
import whisper  # noqa: E402
from sklearn.mixture import GaussianMixture  # noqa: E402
from sv import SV  # noqa: E402

rng = np.random.default_rng(0)
t2s = opencc.OpenCC("t2s")
asr = whisper.load_model(A.whisper, device="cpu")
sv = SV("cpu", False)


def load(path):
    return librosa.load(path, sr=16000, mono=True)[0]


def transcribe(y):
    r = asr.transcribe(y, language="zh", fp16=False, temperature=0.0, condition_on_previous_text=False, initial_prompt="以下是普通话的句子。")
    return re.sub(r"[^一-鿿A-Za-z0-9]", "", t2s.convert(r["text"])).lower()


def cer(ref, hyp):
    d = list(range(len(hyp) + 1))
    for i, r in enumerate(ref, 1):
        prev, d[0] = d[0], i
        for j, h in enumerate(hyp, 1):
            prev, d[j] = d[j], min(d[j] + 1, d[j - 1] + 1, prev + (r != h))
    return d[-1] / max(len(ref), 1)


def embed(y):
    w = torch.from_numpy(y).float()[None]
    e = sv.compute_embedding3(w)[0]
    return torch.nn.functional.normalize(e, dim=0)


def mfcc(y):
    m = librosa.feature.mfcc(y=y, sr=16000, n_mfcc=20, n_fft=400, hop_length=160)[1:]
    rms = librosa.feature.rms(y=y, frame_length=400, hop_length=160)[0]
    db = 20 * np.log10(rms + 1e-6)
    return m.T[: len(db)][db[: m.shape[1]] > db.max() - 35]


def f0(y):
    f, voiced, _ = librosa.pyin(y, fmin=60, fmax=700, sr=16000, frame_length=1024, hop_length=160)
    return np.where(voiced, np.nan_to_num(f), 0.0)


def frames(y):
    x = librosa.feature.mfcc(y=y, sr=16000, n_mfcc=13, n_fft=400, hop_length=160)[1:]
    return (x - x.mean(1, keepdims=True)) / (x.std(1, keepdims=True) + 1e-6)


def intonation(src, y, src_f0, y_f0):
    """src、y 为波形。返回对齐后的对数音高相关；可比较的帧太少返回 None。"""
    _, path = librosa.sequence.dtw(frames(src), frames(y), metric="cosine")
    i, j = path[::-1, 0], path[::-1, 1]
    ok = (i < len(src_f0)) & (j < len(y_f0))
    a, b = src_f0[i[ok]], y_f0[j[ok]]
    ok = (a > 0) & (b > 0)
    if ok.sum() < 20 or np.std(a[ok]) == 0 or np.std(b[ok]) == 0:
        return None
    return float(np.corrcoef(np.log(a[ok]), np.log(b[ok]))[0, 1])


def speech_sec(y):
    return len(librosa.effects.trim(y, top_db=35)[0]) / 16000


# ---------- 目标音色基准 ----------
rows = [l.rstrip("\n").split("|", 3) for l in open(CFG["ref_list"], encoding="utf-8") if l.strip()]
paths = [os.path.join(CFG["ref_base"], r[0]) for r in rows]
order = rng.permutation(len(paths))
held = [paths[i] for i in order[: len(paths) // 5]]
base = [paths[i] for i in order[len(paths) // 5:]]
print(f"目标音色：{len(base)} 条建基准，{len(held)} 条作真人对照", flush=True)
centroid = torch.nn.functional.normalize(torch.stack([embed(load(p)) for p in base]).mean(0), dim=0)
gmm = GaussianMixture(32, covariance_type="diag", reg_covar=1e-3, random_state=0).fit(np.concatenate([mfcc(load(p)) for p in base]))


def voice(y, f):
    return {"sv": float(embed(y) @ centroid), "gmm": float(gmm.score(mfcc(y))), "f0": float(np.median(f[f > 0])) if (f > 0).any() else None}


cache = {}
if os.path.exists(A.out):
    with open(A.out, encoding="utf-8") as h:
        old = json.load(h)
    for label, items in [("source", old.get("source", [])), *old.get("systems", {}).items()]:
        cache.update({(label, x["name"]): x["text"] for x in items})


def heard_text(label, name, y):
    return cache[(label, name)] if (label, name) in cache else transcribe(y)


result = {"target_real": [], "source": [], "systems": {k: [] for k in SYSTEMS}}
for p in held:
    y = load(p)
    result["target_real"].append(voice(y, f0(y)))

files = sorted(glob.glob(os.path.join(A.source, "*.wav")))
sources = [load(p) for p in files]
source_f0 = [f0(y) for y in sources]
for n, path in enumerate(files):
    name = os.path.basename(path)
    src, sf0 = sources[n], source_f0[n]
    other = (n + len(files) // 2) % len(files)  # 错配对照用的另一条输入
    text, dur = heard_text("source", name, src), speech_sec(src)
    result["source"].append({"name": name, "text": text, **voice(src, sf0)})
    for label, folder in SYSTEMS.items():
        y = load(os.path.join(folder, name))
        f = f0(y)
        heard = heard_text(label, name, y)
        result["systems"][label].append({"name": name, "text": heard, "cer": min(cer(text, heard), 1.0), "corr": intonation(src, y, sf0, f),
                                         "corr_mismatch": intonation(sources[other], y, source_f0[other], f), "dur_ratio": speech_sec(y) / dur, **voice(y, f)})
    print(f"[{n + 1}/{len(files)}] {name}", flush=True)


# ---------- 汇总 ----------
def values(items, key):
    return np.array([x[key] for x in items if x.get(key) is not None], dtype=float)


def ci(v):
    """均值的 95% 自助法置信区间。"""
    m = rng.choice(v, (10000, len(v))).mean(1)
    return [float(v.mean()), float(np.percentile(m, 2.5)), float(np.percentile(m, 97.5))]


summary = {}
for label, items in [("真人目标录音", result["target_real"]), ("输入原声", result["source"]), *result["systems"].items()]:
    s = {"n": len(items)}
    for key in ("cer", "sv", "gmm", "corr", "corr_mismatch", "f0", "dur_ratio"):
        v = values(items, key)
        if len(v):
            s[key] = ci(v)
    if any("cer" in x for x in items):
        s["cer_median"] = float(np.median(values(items, "cer")))
        s["broken"] = int((values(items, "cer") > 0.5).sum())
        s["dur_median"] = float(np.median(values(items, "dur_ratio")))
        s["dur_off"] = int((np.abs(values(items, "dur_ratio") - 1) > 0.15).sum())  # 时长偏差超过 15% 的条数
        s["corr_median"] = float(np.median(values(items, "corr")))
    summary[label] = s
result["summary"] = summary
with open(A.out, "w", encoding="utf-8") as h:
    json.dump(result, h, ensure_ascii=False, indent=1)
for label, s in summary.items():
    print(f"\n{label}  n={s['n']}")
    for k, v in s.items():
        if k != "n":
            print(f"  {k:10s}", " / ".join(f"{x:.3f}" for x in v) if isinstance(v, list) else v)
