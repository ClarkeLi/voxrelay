"""读取 config.json（格式见 config.example.json）。
cache_dir 的相对路径以本仓库为起点，其余相对路径以 GPT-SoVITS 目录为起点。
"""
import hashlib, json, os, sys

HOME = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # 本仓库根目录
CONFIG = os.path.abspath(os.environ.get("VOXRELAY_CONFIG") or os.path.join(HOME, "config.json"))
DEFAULTS = {
    "gpt_sovits_dir": "", "version": "v2Pro", "gpt_weights": "", "sovits_weights": "", "device": "cpu",
    "ref_list": "", "ref_base": "", "realtime_ref": "", "default_ref": None, "base_rate": 3.8,
    "asr_dir": "tools/asr/models", "cache_dir": ".cache", "port": 9881,
    "remote": {"target": "", "device": "BlackHole 2ch"},
}


def raw():
    """配置文件内容叠加默认值。文件不存在时全用默认值（远程声卡自检不需要模型配置）。"""
    data = {}
    if os.path.exists(CONFIG):
        try:
            with open(CONFIG, encoding="utf-8") as f:
                data = json.load(f)
        except ValueError as e:
            sys.exit(f"{CONFIG} 不是合法的 JSON：{e}")
    c = {**DEFAULTS, **data}
    c["remote"] = {**DEFAULTS["remote"], **(data.get("remote") or {})}
    return c


def _path(base, p):
    return os.path.normpath(os.path.join(base, os.path.expanduser(p)))


def cache_dir():
    return _path(HOME, raw()["cache_dir"])


def data_dir(ref_list):
    """与某个参考列表绑定的缓存（语气索引、体检结果），换列表不会串。"""
    return os.path.join(cache_dir(), "refs-" + hashlib.sha1(ref_list.encode()).hexdigest()[:8])


def server():
    """启动服务需要的完整配置，路径都换成绝对路径；缺什么直接说明。"""
    c = raw()
    if not os.path.exists(CONFIG):
        sys.exit(f"找不到 {CONFIG}。请复制 config.example.json 为 config.json 后填写。")
    for key in ("gpt_sovits_dir", "ref_list"):
        if not c[key]:
            sys.exit(f"config.json 里缺少 {key}")
    gsv = c["gpt_sovits_dir"] = _path(HOME, c["gpt_sovits_dir"])
    if not os.path.isdir(os.path.join(gsv, "GPT_SoVITS")):
        sys.exit(f"gpt_sovits_dir 不是 GPT-SoVITS 目录：{gsv}")
    c["ref_list"] = _path(gsv, c["ref_list"])
    c["ref_base"] = _path(gsv, c["ref_base"]) if c["ref_base"] else os.path.dirname(c["ref_list"])
    if not os.path.isfile(c["ref_list"]):
        sys.exit(f"找不到参考列表：{c['ref_list']}")
    c["asr_dir"] = _path(gsv, c["asr_dir"])
    if c["default_ref"]:
        c["default_ref"] = (_path(gsv, c["default_ref"]["path"]), c["default_ref"].get("text", ""))
    # 没指定权重时沿用 GPT-SoVITS WebUI 记录的当前权重（weight.json）
    for key, name in (("gpt_weights", "GPT"), ("sovits_weights", "SoVITS")):
        if not c[key]:
            try:
                with open(os.path.join(gsv, "weight.json"), encoding="utf-8") as f:
                    c[key] = json.load(f)[name][c["version"]]
            except (OSError, ValueError, KeyError):
                sys.exit(f"config.json 里没有 {key}，GPT-SoVITS 的 weight.json 里也没有 {c['version']} 的记录")
        c[key] = _path(gsv, c[key])
        if not os.path.isfile(c[key]):
            sys.exit(f"找不到权重文件：{c[key]}")
    c["cache_dir"] = cache_dir()
    c["data_dir"] = data_dir(c["ref_list"])
    return c
