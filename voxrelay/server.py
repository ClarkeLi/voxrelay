"""VoxRelay 服务端。页面在 web/ 目录。
实时：浏览器断句 → /api/rt/asr 识别 → /api/rt/synth 流式合成（边生成边回传 PCM）→ 浏览器播放。
精调：说一句话 → 识别内容 + 分析语气 → 在参考录音里找语气最像的一条 → 用你的 GPT-SoVITS 权重合成。
语气特征只用说话人无关的量（音高起伏/句尾升降/语速/停顿/响度动态/时长 + 文本线索），不用绝对音高和音量。
抽卡（/api/roll，每次合成一条）：参考按语气距离加权随机抽、种子随机、无参考文本，合成后回读打分；页面负责过滤和排序。
启动: ./start.sh，浏览器打开 http://127.0.0.1:9881/ （localhost 才允许使用麦克风）。
"""
import difflib, glob, hashlib, json, math, os, random, re, subprocess, sys, tempfile, threading, time, traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from multiprocessing.pool import ThreadPool
from urllib.parse import urlparse, parse_qs, unquote

from . import config, relay

CFG = config.server()
GSV = CFG["gpt_sovits_dir"]
os.chdir(GSV)  # GPT-SoVITS 内部按它自己的根目录找文件
sys.path.insert(0, GSV)
sys.path.insert(0, os.path.join(GSV, "GPT_SoVITS"))
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
CACHE = CFG["cache_dir"]
DATA = CFG["data_dir"]  # 当前参考列表的语气索引和体检结果
os.makedirs(DATA, exist_ok=True)
os.environ.setdefault("NUMBA_CACHE_DIR", os.path.join(CACHE, "numba"))
os.environ.setdefault("MPLCONFIGDIR", os.path.join(CACHE, "mpl"))
os.environ["TMPDIR"] = CACHE
tempfile.tempdir = CACHE

import numpy as np, soundfile as sf, librosa  # noqa: E402

WEB = os.path.join(config.HOME, "web")
STATIC_TYPES = {".html": "text/html", ".css": "text/css", ".js": "text/javascript", ".svg": "image/svg+xml"}
REF_LIST = CFG["ref_list"]
REF_BASE = CFG["ref_base"]
DEFAULT_REF = CFG["default_ref"]  # (路径, 文本) 或 None：精调页的对比项
_lock = threading.Lock()
_asr_lock = threading.Lock()
_tts = None
_asr = None
_index = None
_stable = None  # 体检通过的参考名集合（scripts/audit_refs.py 生成）
_stable_mtime = 0
ROLL_TOP, ROLL_T = 30, 1.5  # 抽卡：只在语气距离前 30 的参考里抽，权重 exp(-(距离 - 最小距离) / ROLL_T)
_contours = {}  # src_id -> 源语音音高轮廓
RT_REF = CFG["realtime_ref"]  # 实时模式默认参考；留空时取体检成绩最好的一条
BASE_RATE = float(CFG["base_rate"])  # 模型自然语速（字/秒），用于“说得快时跟着加快”
RELAY_PORT = 0  # 远程声卡通道的端口（页面端口 + 1），0 表示没开

# 特征：key -> (中文名, 权重)。全部是说话人无关的相对量
FEATS = {
    "f0_std": ("音高起伏", 1.0), "f0_range": ("音高跨度", 1.0), "tail": ("句尾升降", 1.2),
    "db_std": ("响度动态", 0.8), "rate": ("语速", 1.2), "pause": ("停顿比例", 0.8), "dur": ("时长", 0.5),
    "char_count": ("字数", 0.4),
}
CUES = {"question": ("疑问", 1.2), "exclaim": ("感叹", 0.8), "tired": ("疲惫", 0.6), "annoyed": ("烦躁", 0.6), "positive": ("积极", 0.4)}


def text_cues(text):
    t = text or ""
    return {
        "question": float(bool(re.search(r"[?？]|(吗|呢|咋|怎么|为什么|啥|难道)", t))),
        "exclaim": float(bool(re.search(r"[!！]|(啊|呀|哎|我去|气死|好烦|服了|不行|太)", t))),
        "tired": float(bool(re.search(r"(累|困|乏|心累|睡|倒头)", t))),
        "annoyed": float(bool(re.search(r"(烦|气|臭|服了|啥意思|不行|抢不到|坐不下|讨厌)", t))),
        "positive": float(bool(re.search(r"(好|谢谢|开心|喜欢|明天见|欢迎|嘻嘻|哈哈|爱)", t))),
    }


def nchars(text):
    return len(re.findall(r"[一-鿿A-Za-z0-9]", text or ""))


def prosody(y, sr, text):
    """从 16k 单声道波形提取韵律特征；音高不可靠时对应项为 None。"""
    hop = 160
    rms = librosa.feature.rms(y=y, frame_length=400, hop_length=hop)[0]
    db = 20 * np.log10(rms + 1e-6)
    thr = max(float(db.max()) - 35.0, -70.0)
    voiced = db > thr
    idx = np.where(voiced)[0]
    if len(idx) < 20:
        raise ValueError("没有检测到有效语音（太短或太安静）")
    a, b = int(idx[0]), int(idx[-1])
    seg = voiced[a:b + 1]
    dur = (b - a + 1) * hop / sr
    out = {"dur": dur, "pause": float(1 - seg.mean()), "db_std": float(db[a:b + 1][seg].std()),
           "rate": nchars(text) / dur if text else None, "char_count": float(nchars(text)) if text else None,
           "f0_std": None, "f0_range": None, "tail": None}
    ys = y[a * hop:(b + 1) * hop]
    try:
        f0, vf, _ = librosa.pyin(ys, fmin=65, fmax=500, sr=sr, frame_length=1024, hop_length=320)
        f = f0[~np.isnan(f0)]
        if len(f) >= 15:
            st = 12 * np.log2(f / np.median(f))
            k = max(5, len(st) // 5)
            out.update(f0_std=float(st.std()), f0_range=float(np.percentile(st, 90) - np.percentile(st, 10)), tail=float(st[-k:].mean()))
    except Exception:
        traceback.print_exc()
    return out


def load16k(path):
    y, sr = librosa.load(path, sr=16000, mono=True)
    return y, 16000


def build_index():
    global _index
    meta = {"mtime": os.path.getmtime(REF_LIST), "v": 3}
    cf = os.path.join(DATA, "index.json")
    if os.path.exists(cf):
        try:
            c = json.load(open(cf))
            if c.get("meta") == meta and c.get("items"):
                _index = c
                return
        except Exception:
            pass
    rows = [l.rstrip("\n").split("|", 3) for l in open(REF_LIST, encoding="utf8")]
    jobs = []
    for p, _, _, t in rows:
        full = os.path.join(REF_BASE, p)
        try:
            info = sf.info(full)
        except Exception:
            continue
        if 3.0 <= info.duration <= 10.0:
            jobs.append((p, full, t))
    print(f"首次建立参考语气索引：{len(jobs)} 条（约几分钟，之后读缓存）…", flush=True)

    def work(j):
        p, full, t = j
        try:
            y, sr = load16k(full)
            f = prosody(y, sr, t)
        except Exception:
            return None
        return {"name": os.path.basename(p), "path": os.path.relpath(full, GSV), "text": t, "feats": f, "cues": text_cues(t)}

    with ThreadPool(4) as pool:
        items = [r for r in pool.map(work, jobs) if r]
    if not items:
        sys.exit(f"{REF_LIST} 里没有可用的参考录音（需要 3–10 秒、能读到文件）")
    stats = {}
    for k in FEATS:
        v = np.array([it["feats"][k] for it in items if it["feats"].get(k) is not None], dtype=float)
        stats[k] = {"mean": float(v.mean()), "std": float(v.std() + 1e-6), "sorted": sorted(v.tolist())}
    _index = {"meta": meta, "items": items, "stats": stats}
    json.dump(_index, open(cf, "w"), ensure_ascii=False)
    print(f"索引完成：{len(items)} 条", flush=True)


def z(k, v):
    s = _index["stats"][k]
    return max(-3.0, min(3.0, (v - s["mean"]) / s["std"]))


def pct(k, v):
    arr = _index["stats"][k]["sorted"]
    return sum(1 for x in arr if x < v) / len(arr)


def describe(feats, cues, has_audio):
    tags = []
    if has_audio:
        def lab(k, lo, hi, p_lo=0.25, p_hi=0.75):
            v = feats.get(k)
            if v is None:
                return
            p = pct(k, v)
            if p >= p_hi:
                tags.append(hi)
            elif p <= p_lo:
                tags.append(lo)
        lab("rate", "语速偏慢", "语速偏快"); lab("f0_range", "语调平稳", "语调起伏大"); lab("tail", "句尾下沉", "句尾上扬")
        lab("db_std", "力度平均", "力度变化大"); lab("pause", "几乎无停顿", "停顿较多")
    tags += [CUES[k][0] + "语气" for k, v in cues.items() if v]
    return tags or ["中性平稳"]


def load_stable():
    global _stable, _stable_mtime
    f = os.path.join(DATA, "stable.json")
    if os.path.exists(f) and os.path.getmtime(f) != _stable_mtime:
        _stable = set(json.load(open(f))); _stable_mtime = os.path.getmtime(f)
    return _stable


def rank(feats, cues, has_audio, topn=5):
    res = []
    st = load_stable()
    pool = _index["items"]
    for it in pool:
        d, parts = 0.0, []
        for k, (zh, w) in FEATS.items():
            if k in ("f0_std", "f0_range", "tail", "db_std", "pause", "dur") and not has_audio:
                continue
            u, r = feats.get(k), it["feats"].get(k)
            if u is None or r is None:
                continue
            c = w * abs(z(k, u) - z(k, r))
            d += c; parts.append((zh, c))
        for k, (zh, w) in CUES.items():
            c = w * abs(cues[k] - it["cues"][k]) * 1.5
            d += c
            if c: parts.append((zh + "线索", c))
        res.append((d, it, parts))
    res.sort(key=lambda x: x[0])
    return [{"name": it["name"], "path": it["path"], "text": it["text"], "score": round(d, 3), "dur": round(it["feats"]["dur"], 1), "stable": bool(st) and it["name"] in st,
             "tags": describe(it["feats"], it["cues"], True),
             "worst": [p[0] for p in sorted(parts, key=lambda p: -p[1])[:2] if p[1] > 0.3]} for d, it, parts in res[:topn]]


def get_asr():
    global _asr
    if _asr is None:
        from funasr import AutoModel
        names = ["speech_paraformer-large_asr_nat-zh-cn-16k-common-vocab8404-pytorch", "speech_fsmn_vad_zh-cn-16k-common-pytorch",
                 "punc_ct-transformer_zh-cn-common-vocab272727-pytorch"]
        model, vad, punc = (os.path.join(CFG["asr_dir"], n) for n in names)
        missing = [p for p in (model, vad, punc) if not os.path.isdir(p)]
        if missing:
            raise FileNotFoundError("缺少识别模型：" + "、".join(missing))
        _asr = AutoModel(model=model, model_revision="v2.0.4", vad_model=vad, vad_model_revision="v2.0.4",
                         punc_model=punc, punc_model_revision="v2.0.4", device=CFG["device"], disable_update=True)
    return _asr


def get_tts():
    global _tts
    if _tts is None:
        from TTS_infer_pack.TTS import TTS, TTS_Config
        cfg = TTS_Config({"custom": {"device": CFG["device"], "is_half": False, "version": CFG["version"],
                                     "t2s_weights_path": CFG["gpt_weights"], "vits_weights_path": CFG["sovits_weights"],
                                     "cnhuhbert_base_path": "GPT_SoVITS/pretrained_models/chinese-hubert-base",
                                     "bert_base_path": "GPT_SoVITS/pretrained_models/chinese-roberta-wwm-ext-large"}})
        cfg.configs_path = os.path.join(CACHE, "tts_infer.yaml")  # 不改 GPT-SoVITS 里受版本管理的配置
        _tts = TTS(cfg)
        print("合成权重：", CFG["sovits_weights"], "+", CFG["gpt_weights"], flush=True)
    return _tts


def keep_src(wav):
    """把分析过的 16k 源语音保留下来供「直转」使用，只留最近 6 个。"""
    sid = hashlib.sha1(open(wav, "rb").read()).hexdigest()[:16]
    dst = f"{CACHE}/src_{sid}.wav"
    if not os.path.exists(dst):
        os.replace(wav, dst)
    olds = sorted(glob.glob(CACHE + "/src_*.wav"), key=os.path.getmtime)[:-6]
    for o in olds:
        os.remove(o)
    return sid


def analyze_audio(raw):
    src = tempfile.mktemp(dir=CACHE, suffix=".in")
    wav = tempfile.mktemp(dir=CACHE, suffix=".wav")
    try:
        open(src, "wb").write(raw)
        r = subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", src, "-ar", "16000", "-ac", "1", wav], capture_output=True)
        if r.returncode != 0:
            raise ValueError("音频解码失败：" + r.stderr.decode(errors="ignore")[:200])
        with _asr_lock:
            res = get_asr().generate(input=wav)
        text = (res[0].get("text") or "").strip() if res else ""
        y, sr = load16k(wav)
        feats = prosody(y, sr, text)
        sid = keep_src(wav)
    finally:
        for p in (src, wav):
            if os.path.exists(p):
                os.remove(p)
    cues = text_cues(text)
    return {"text": text, "feats": feats, "cues": cues, "tags": describe(feats, cues, True), "has_audio": True, "src_id": sid, "top": rank(feats, cues, True)}


def analyze_text(text):
    feats = {"char_count": float(nchars(text)), "rate": None, "dur": None}
    cues = text_cues(text)
    return {"text": text, "feats": feats, "cues": cues, "tags": describe(feats, cues, False), "has_audio": False, "top": rank(feats, cues, False)}


def allowed_ref(rel):
    """页面只能读取、使用索引里的参考录音和默认参考。"""
    full = os.path.abspath(os.path.join(GSV, rel))
    known = {os.path.abspath(os.path.join(GSV, it["path"])) for it in _index["items"]}
    if DEFAULT_REF:
        known.add(os.path.abspath(DEFAULT_REF[0]))
    return full if full in known else None


def speech_dur(w, sr):
    y = w.astype(np.float32) / (32768.0 if w.dtype == np.int16 else 1.0)
    yt, _ = librosa.effects.trim(y, top_db=35)
    return len(yt) / sr


def synth(req):
    """mode=tts：GSV 常规合成（稳定参考→带参考文本传语气；match_rate+target_dur→两遍合成匹配语速）；mode=vc：语义 token 直转（实验）。"""
    text = (req.get("text") or "").strip()
    if not text:
        raise ValueError("文本为空")
    if req.get("ref") == "__default__":
        if not DEFAULT_REF:
            raise ValueError("没有配置默认参考")
        ref, ptext = DEFAULT_REF
    else:
        ref = allowed_ref(req["ref"])
        if not ref or not os.path.exists(ref):
            raise ValueError("参考音频不允许")
        ptext = next((it["text"] for it in _index["items"] if os.path.abspath(os.path.join(GSV, it["path"])) == ref), "")
    mode = req.get("mode", "tts")
    use_prompt = bool(req.get("use_prompt"))
    seed = int(req.get("seed", 42))
    target = float(req["target_dur"]) if req.get("match_rate") and req.get("target_dur") else None
    sid = req.get("src_id", "")
    if mode == "vc" and not re.fullmatch(r"[0-9a-f]{16}", sid or ""):
        raise ValueError("直转需要先录音/上传并分析")
    key = hashlib.sha1(json.dumps([text, ref, use_prompt, seed, mode, target, sid, "v4", CFG["gpt_weights"], CFG["sovits_weights"]]).encode()).hexdigest()[:20]
    out, meta = f"{CACHE}/{key}.wav", f"{CACHE}/{key}.json"
    if os.path.exists(out) and os.path.exists(meta):
        m = json.load(open(meta)); m.update(cached=True, url=f"/audio/{key}.wav")
        return m

    def run(tts, speed, prompt=None):
        prompt = use_prompt if prompt is None else prompt
        return next(tts.run({"text": text, "text_lang": "zh", "ref_audio_path": ref, "prompt_text": ptext if prompt else "",
                             "prompt_lang": "zh", "text_split_method": "cut5", "seed": seed, "streaming_mode": False,
                             "speed_factor": speed, "fragment_interval": 0.25}))

    info = {}
    with _lock:
        tts = get_tts()
        t0 = time.time()
        if mode == "vc":
            from . import voice_convert
            src = f"{CACHE}/src_{sid}.wav"
            if not os.path.exists(src):
                raise ValueError("源语音已过期，请重新录音")
            sr, w = voice_convert.vc(tts, src, text, ref)
        else:
            sr, w = run(tts, 1.0)
            d1 = speech_dur(w, sr)
            rate = nchars(text) / max(d1, 0.1)
            if use_prompt and not (1.2 <= rate <= 7.5):  # 带参考文本偶发崩坏（只出一小段声音/拖很长）：回退到稳定的无参考文本模式
                info["fallback"] = f"带参考文本输出异常（{rate:.1f}字/秒），已自动回退"
                use_prompt = False
                sr, w = run(tts, 1.0, prompt=False)
                d1 = speech_dur(w, sr)
            if target:
                sp = max(0.8, min(1.25, d1 / target))
                info["speed"] = round(sp, 3); info["natural_dur"] = round(d1, 2)
                if abs(sp - 1) > 0.06:
                    sr, w = run(tts, sp)
        el = time.time() - t0
    sf.write(out, w, sr)
    m = {"dur": len(w) / sr, "sec": el, **info}
    json.dump(m, open(meta, "w"))
    m.update(cached=False, url=f"/audio/{key}.wav")
    return m


def pitch_contour(y):
    """16k 波形 → 归一化的 100 点音高轮廓（只看走向，与绝对音高无关）；有声帧太少返回 None。"""
    f0, _, _ = librosa.pyin(y, fmin=65, fmax=600, sr=16000, frame_length=1024, hop_length=320)
    ok = ~np.isnan(f0)
    if ok.sum() < 15:
        return None
    i = np.arange(len(f0))
    st = 12 * np.log2(np.interp(i, i[ok], f0[ok]) / np.median(f0[ok]))[i[ok][0]:i[ok][-1] + 1]
    c = np.interp(np.linspace(0, len(st) - 1, 100), np.arange(len(st)), st)
    return (c - c.mean()) / (c.std() + 1e-6)


def read_back(y):
    wav = tempfile.mktemp(dir=CACHE, suffix=".wav")
    try:
        sf.write(wav, y, 16000, subtype="PCM_16")
        with _asr_lock:
            res = get_asr().generate(input=wav)
        return (res[0].get("text") or "").strip() if res else ""
    finally:
        if os.path.exists(wav):
            os.remove(wav)


def score_wav(path, text, src_contour=None, target_dur=None):
    """给一条合成结果打分：音高轮廓相关 - |ln 时长比| - 2×CER。没有源语音时只看 CER。"""
    y, _ = load16k(path)
    heard = read_back(y)
    norm = lambda s: re.sub(r"[^一-鿿A-Za-z0-9]", "", s).lower()
    cer = 1 - difflib.SequenceMatcher(None, norm(text), norm(heard)).ratio()
    ratio = corr = None
    if target_dur:
        try:
            ratio = prosody(y, 16000, heard)["dur"] / target_dur
        except ValueError:  # 合成结果里没有有效语音
            ratio, cer = 0.05, 1.0
    if src_contour is not None:
        c = pitch_contour(y)
        if c is not None:
            v = float(np.corrcoef(src_contour, c)[0, 1])
            corr = v if math.isfinite(v) else None  # 完全平直的轮廓会得到 NaN，浏览器解析不了
    score = (corr or 0.0) - (abs(math.log(max(ratio, 0.05))) if ratio is not None else 0.0) - 2 * cer
    return {"heard": heard, "cer": cer, "ratio": ratio, "corr": corr, "score": score}


def roll(req):
    """抽一张卡：加权随机选参考 + 随机种子 → 合成（无参考文本）→ 回读打分。页面循环调用，便于逐条显示和中途停止。"""
    text = (req.get("text") or "").strip()
    if not text:
        raise ValueError("文本为空")
    feats, cues, sid = req["feats"], req["cues"], req.get("src_id") or ""
    has_audio = bool(sid)
    contour, target = None, None
    if has_audio:
        src = f"{CACHE}/src_{sid}.wav"
        if not re.fullmatch(r"[0-9a-f]{16}", sid) or not os.path.exists(src):
            raise ValueError("源语音已过期，请重新录音")
        target = float(feats["dur"])
        if sid not in _contours:
            _contours[sid] = pitch_contour(load16k(src)[0])
        contour = _contours[sid]
    pool = rank(feats, cues, has_audio, topn=ROLL_TOP)
    ref = random.choices(pool, weights=[math.exp(-(c["score"] - pool[0]["score"]) / ROLL_T) for c in pool])[0]
    seed = random.randint(1, 10 ** 6)
    t0 = time.time()
    r = synth(dict(text=text, ref=ref["path"], seed=seed, use_prompt=False, match_rate=bool(req.get("match_rate")), target_dur=target))
    m = score_wav(os.path.join(CACHE, os.path.basename(r["url"])), text, contour, target)
    return {**m, "url": r["url"], "dur": r["dur"], "speed": r.get("speed"), "sec": time.time() - t0, "seed": seed,
            "ref": {"name": ref["name"], "text": ref["text"], "path": ref["path"]}}


def rt_refs():
    """可选参考：体检通过的稳定参考（按平均 CER 从低到高）；没有体检结果时退回全部。"""
    f = os.path.join(DATA, "audit.jsonl")
    means = {}
    if os.path.exists(f):
        means = {r["name"]: r["mean"] for r in map(json.loads, open(f, encoding="utf8"))}
    st = load_stable()
    pool = [it for it in _index["items"] if not st or it["name"] in st]
    return sorted(pool, key=lambda it: (it["name"] != RT_REF, means.get(it["name"], 1.0)))


def rt_config():
    return {"refs": [{"name": it["name"], "text": it["text"], "path": it["path"], "dur": round(it["feats"]["dur"], 1)} for it in rt_refs()],
            "weights": {"SoVITS": os.path.basename(CFG["sovits_weights"]), "GPT": os.path.basename(CFG["gpt_weights"])},
            "default_ref": bool(DEFAULT_REF), "relay_port": RELAY_PORT, "remote": CFG["remote"]}


def rt_asr(body, q):
    """请求体是 16k 单声道 int16 PCM。边说边出字和整句定稿共用；整句时附带语速，需要时按语气挑参考。"""
    if len(body) < 3200 or len(body) > 16000 * 2 * 60 or len(body) % 2:
        raise ValueError("音频长度不合法（0.1–60 秒的 16k int16 PCM）")
    y = np.frombuffer(body, dtype="<i2").astype(np.float32) / 32768.0
    t0 = time.time()
    text = read_back(y)
    out = {"text": text, "asr_sec": time.time() - t0}
    if q.get("final") and nchars(text):
        dur = speech_dur(y, 16000)
        out.update(dur=dur, rate=nchars(text) / max(dur, 0.1))
        if q.get("auto_ref"):
            try:
                feats = prosody(y, 16000, text)
                names = {it["name"] for it in rt_refs()}
                out["ref"] = next((c["name"] for c in rank(feats, text_cues(text), True, topn=len(_index["items"])) if c["name"] in names), None)
            except ValueError:
                pass
    return out


def rt_params(req):
    text = (req.get("text") or "").strip()
    if not nchars(text) or len(text) > 400:
        raise ValueError("文本为空或过长")
    items = {it["name"]: it for it in _index["items"]}
    it = items.get(req.get("ref") or RT_REF) or items.get(RT_REF) or rt_refs()[0]
    speed = float(req.get("speed") or 1.0)
    rate = float(req.get("rate") or 0)
    if req.get("follow_rate") and rate > 0 and nchars(text) >= 5:  # 太短的句子语速量不准
        speed *= max(1.0, min(1.2, rate / BASE_RATE))  # 只加快不放慢：合成比原话长的话，分段播放会越落越远
    speed = max(0.6, min(1.6, speed))
    seed = req.get("seed")
    seed = random.randint(1, 10 ** 6) if seed in (None, "", -1) else int(seed)
    return text, it, speed, seed


def rt_stream(req):
    """生成器：先给 (sr, 参考名, 种子, 语速)，之后逐块给 int16 PCM。调用方负责关闭（取消时立即停止生成）。"""
    text, it, speed, seed = rt_params(req)
    with _lock:
        gen = get_tts().run({"text": text, "text_lang": "zh", "ref_audio_path": os.path.join(GSV, it["path"]), "prompt_text": "", "prompt_lang": "zh",
                             "text_split_method": "cut5", "seed": seed, "speed_factor": speed, "fragment_interval": 0.05 if req.get("cont") else 0.2,  # 话没说完的段不留长尾静音
                             "streaming_mode": True, "parallel_infer": False, "min_chunk_length": 16})
        try:
            first = True
            for sr, w in gen:
                if first:
                    yield sr, it["name"], seed, speed
                    first = False
                yield w.astype("<i2").tobytes()
        finally:
            gen.close()


class H(BaseHTTPRequestHandler):
    timeout = 60  # 客户端长时间不读数据时断开，避免占住合成锁

    def log_message(self, format, *args):
        pass

    def parse_request(self):
        """只认本机地址的 Host 头，挡住用别的域名指向 127.0.0.1 来读数据的网页（DNS rebinding）。"""
        if not super().parse_request():
            return False
        port = self.server.server_address[1]
        if self.headers.get("Host") in (f"127.0.0.1:{port}", f"localhost:{port}"):
            return True
        self.send_error(403)
        return False

    def _send(self, code, body, ctype="application/json"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False).encode()
        elif isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _stream(self, req):
        """分帧回传：每帧 4 字节小端长度 + PCM；长度 0 表示正常结束（没收到它说明中途断了）。"""
        gen = rt_stream(req)
        try:
            sr, ref, seed, speed = next(gen)  # 这一步出错时还没发响应头，由 do_POST 返回 500
            try:
                self.send_response(200)
                for k, v in (("Content-Type", "application/octet-stream"), ("Cache-Control", "no-store"), ("X-Sample-Rate", sr),
                             ("X-Ref", ref), ("X-Seed", seed), ("X-Speed", f"{speed:.3f}")):
                    self.send_header(k, str(v))
                self.end_headers()
                for chunk in gen:
                    if chunk:
                        self.wfile.write(len(chunk).to_bytes(4, "little") + chunk)
                self.wfile.write((0).to_bytes(4, "little"))
            except (BrokenPipeError, ConnectionResetError, TimeoutError):
                pass  # 页面取消了这一句
            except Exception:
                traceback.print_exc()  # 响应头已发出，只能断开
        finally:
            gen.close()

    def do_GET(self):
        u = urlparse(self.path)
        if u.path == "/" or u.path.startswith("/static/"):
            name = "index.html" if u.path == "/" else u.path[len("/static/"):]
            ext = os.path.splitext(name)[1]
            p = os.path.join(WEB, name)
            if re.fullmatch(r"[a-z0-9_-]+\.[a-z]+", name) and ext in STATIC_TYPES and os.path.isfile(p):
                return self._send(200, open(p, "rb").read(), STATIC_TYPES[ext] + "; charset=utf-8")
        if u.path == "/api/config":
            return self._send(200, rt_config())
        if u.path.startswith("/audio/"):
            name = os.path.basename(unquote(u.path)); p = os.path.join(CACHE, name)
            if re.fullmatch(r"[0-9a-f]{20}\.wav", name) and os.path.exists(p):
                return self._send(200, open(p, "rb").read(), "audio/wav")
        if u.path == "/ref":
            full = allowed_ref(unquote(parse_qs(u.query).get("path", [""])[0]))
            if full and os.path.exists(full):
                return self._send(200, open(full, "rb").read(), "audio/wav")
        self._send(404, {"error": "not found"})

    def do_POST(self):
        try:
            n = int(self.headers.get("Content-Length", 0)); body = self.rfile.read(n)
            u = urlparse(self.path)
            if u.path == "/api/rt/asr":
                return self._send(200, rt_asr(body, {k: v[0] == "1" for k, v in parse_qs(u.query).items()}))
            if u.path == "/api/rt/synth":
                return self._stream(json.loads(body))
            if self.path == "/api/analyze":
                return self._send(200, analyze_audio(body))
            if self.path == "/api/analyze_text":
                return self._send(200, analyze_text(json.loads(body)["text"]))
            if self.path == "/api/synth":
                return self._send(200, synth(json.loads(body)))
            if self.path == "/api/roll":
                return self._send(200, roll(json.loads(body)))
            self._send(404, {"error": "not found"})
        except Exception as e:
            traceback.print_exc()
            self._send(500, {"error": f"{type(e).__name__}: {e}"})


def serve(port=None):
    global RELAY_PORT
    port = port or int(CFG["port"])
    httpd = ThreadingHTTPServer(("127.0.0.1", port), H)  # 先占端口：被占用时不用白等模型加载
    build_index()
    print("加载合成模型和识别模型…", flush=True)
    get_tts(); get_asr()
    print("预热…", flush=True)  # 第一次合成/识别明显更慢，启动时先跑一遍
    for _ in rt_stream({"text": "你好，现在开始。", "seed": 1}):
        pass
    read_back(np.zeros(16000, dtype=np.float32))
    try:  # 远程声卡通道开不起来不影响其他功能
        relay.start("127.0.0.1", port + 1, [f"http://{h}:{port}" for h in ("127.0.0.1", "localhost")])
        RELAY_PORT = port + 1
    except OSError as e:
        print(f"远程声卡通道没有启动（端口 {port + 1}）：{e}", flush=True)
    print(f"就绪: http://127.0.0.1:{port}/", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
