#!/usr/bin/env python
"""由 docs/comparison_summary.json 画 README 里的对比图: python scripts/plot_comparison.py [中文字体文件]"""
import json, os, sys

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib import font_manager  # noqa: E402
from matplotlib.patches import Patch  # noqa: E402

HOME = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FONT = sys.argv[1] if len(sys.argv) > 1 else "/System/Library/Fonts/Hiragino Sans GB.ttc"
font_manager.fontManager.addfont(FONT)
plt.rcParams.update({"font.family": font_manager.FontProperties(fname=FONT).get_name(), "axes.unicode_minus": False})
BLUE, ORANGE, INK, MUTE, GRID = "#257de0", "#f47230", "#191919", "#666666", "#e6e6e6"
with open(os.path.join(HOME, "docs", "comparison_summary.json"), encoding="utf-8") as f:
    data = json.load(f)
S = data["summary"]
LABELS = ["RVC 实时", "RVC 离线", "VoxRelay 实时", "VoxRelay 整段"]
VOX = ["vox_rt", "vox_whole"]


def best(path, key, pick):
    """RVC 试了移调 +10 和 +12，每项取对 RVC 更有利的一档。"""
    return pick((S[f"rvc_{path}_k{k}"][key] for k in (10, 12)), key=lambda v: v[0] if isinstance(v, list) else v)


def series(key, pick):
    return [best("rt", key, pick), best("off", key, pick)] + [S[k][key] for k in VOX]


panels = [
    ("字错率 CER（越低越好）", series("cer", min), 0.62, "{:.2f}", [(data["两个识别器对输入原声的分歧CER"]["均值"], "两个识别器对原声的分歧")]),
    ("音色相似度（越高越好）*", series("sv", max), 1.0, "{:.2f}", [(S["source"]["sv"][0], "输入原声"), (S["target_real"]["sv"][0], "目标真人录音")]),
    ("语调保留：与原声音高相关（越高越好）", series("corr", max), 1.0, "{:.2f}", [(S["vox_whole"]["corr_mismatch"][0], "错配对照（随机水平）")]),
    ("时长偏差超过 15% 的句数，共 40（越低越好）", series("dur_off", min), 24, "{:.0f}", []),
]
fig, axes = plt.subplots(2, 2, figsize=(11, 6.4), dpi=200)
for ax, (title, values, top, fmt, refs) in zip(axes.flat, panels):
    for i, v in enumerate(values):
        mean, err = (v[0], [[v[0] - v[1]], [v[2] - v[0]]]) if isinstance(v, list) else (v, None)
        ax.barh(i, mean, height=0.56, color=ORANGE if i < 2 else BLUE, zorder=2)
        if err:
            ax.errorbar(mean, i, xerr=err, fmt="none", ecolor=INK, elinewidth=1, capsize=3, zorder=3)
        end = v[2] if isinstance(v, list) else v
        ax.text(end + top * 0.015, i, fmt.format(mean), va="center", fontsize=10, color=INK)
    for x, name in refs:
        ax.axvline(x, color=MUTE, lw=1, ls=(0, (4, 3)), zorder=1)
        ax.text(x, -0.62, name, ha="center", va="bottom", fontsize=8, color=MUTE)
    ax.set_title(title, loc="left", fontsize=11, color=INK, pad=18)
    ax.set_yticks(range(4), LABELS, fontsize=10, color=INK)
    ax.set_ylim(3.5, -0.62)
    ax.set_xlim(0, top)
    ax.tick_params(axis="x", labelsize=8, colors=MUTE, length=0)
    ax.tick_params(axis="y", length=0)
    ax.xaxis.grid(True, color=GRID, lw=0.8, zorder=0)
    for side in ax.spines.values():
        side.set_visible(False)
fig.legend(handles=[Patch(color=ORANGE, label="RVC（语音到语音）"), Patch(color=BLUE, label="VoxRelay / GPT-SoVITS（语音 → 文字 → 语音）")],
           loc="upper left", bbox_to_anchor=(0.01, 1.0), ncol=2, frameon=False, fontsize=10)
fig.text(0.012, 0.012, "同源小数据集（约 20 分钟手机聊天语音），40 段输入。误差线为 95% 置信区间。RVC 试了移调 +10、+12，每项取对它更有利的一档。\n* 音色相似度用 ERes2NetV2，GPT-SoVITS v2Pro 合成时也用它，对 VoxRelay 有利。",
         fontsize=8.5, color=MUTE, va="bottom")
fig.tight_layout(rect=(0, 0.07, 1, 0.93), h_pad=2.2, w_pad=3)
fig.savefig(os.path.join(HOME, "docs", "comparison.png"), facecolor="white")
print("docs/comparison.png")
