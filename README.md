# VoxRelay

中文 | [English](README.en.md)

基于 [GPT-SoVITS](https://github.com/RVC-Boss/GPT-SoVITS) 的**实时变声工作台**：你对着麦克风说话，它实时识别成文字，用你训练好的音色重新说出来，播放到扬声器、虚拟声卡，或内网另一台 Mac 的虚拟声卡（给腾讯会议等软件当麦克风）。

```
麦克风 → 断句 → 语音识别 (Paraformer) → 流式合成 (GPT-SoVITS) → 扬声器 / 虚拟声卡 / 远程虚拟声卡
```

它是“语音 → 文字 → 语音”的方案：声音完全由目标音色模型生成，因此有 1.5–3 秒延迟，识别错的字会照错的念出来。

## 功能

- **边说边合**：长句按短停顿或时长上限自动切段，不用等整句说完。
- **语气一致**：同一句切出的各段，以及一个时间窗口内接着说的话，共用同一条参考录音、随机种子和语速。
- **两种触发**：自动断句，或按住空格说话。
- **可干预**：可设置播放前撤回窗口（默认关），`Esc` 撤回，点文字改错字重发。
- **三种输出**：本机扬声器；本机虚拟声卡；通过 SSH 写入内网另一台 Mac 的虚拟声卡。
- **精调页**：录一句话，按语气匹配最像的参考录音；单条合成对比；按语气加权“抽卡”并自动打分排序。

## 环境要求

| 项目 | 说明 |
|---|---|
| 系统 | macOS（在 Apple Silicon M4、CPU 推理下开发和测试） |
| GPT-SoVITS | 单独安装，含 `GPT_SoVITS/pretrained_models`；按 commit `6d51ffc` 开发 |
| 识别模型 | GPT-SoVITS 的 `tools/asr/models` 下的中文 Paraformer、FSMN-VAD、标点三个模型 |
| 音色 | 你自己训练好的 GPT 与 SoVITS 权重（默认按 v2Pro），以及训练用的标注列表和录音 |
| 其他 | `ffmpeg`；Chrome 110 或更新版本 |
| 远程声卡 | 本机需要 Xcode Command Line Tools（用 `swiftc` 编译播放程序） |

目前只支持中文。

## 安装与配置

```bash
git clone <本仓库地址> voxrelay && cd voxrelay
# 在装好 GPT-SoVITS 依赖的 Python 环境里
pip install -r requirements.txt
cp config.example.json config.json
```

如果那个环境不是默认的 `python3`，新建 `local.env`（不进 git）：

```bash
export VOXRELAY_PYTHON=/path/to/env/bin/python
# 可选：缓存和临时目录放到别的盘
# export TMPDIR=/path/tmp HF_HOME=/path/tmp/hf
```

编辑 `config.json`。除 `cache_dir` 以本仓库为起点外，相对路径都以 GPT-SoVITS 目录为起点。

| 字段 | 含义 | 默认 |
|---|---|---|
| `gpt_sovits_dir` | GPT-SoVITS 目录（必填） | |
| `ref_list` | 参考录音列表（必填），GPT-SoVITS 标注格式 `音频路径\|说话人\|语言\|文本`；只用其中 3–10 秒的条目 | |
| `ref_base` | 列表里相对音频路径的起点 | 列表所在目录 |
| `gpt_weights` / `sovits_weights` | 音色权重；留空则读 GPT-SoVITS 的 `weight.json` | 留空 |
| `version` | 模型版本 | `v2Pro` |
| `realtime_ref` | 实时页默认参考的文件名 | 体检成绩最好的一条 |
| `default_ref` | 精调页的对比参考，`{"path": "...", "text": "..."}` | 无 |
| `base_rate` | 你的模型自然语速（字/秒），用于“说得快时跟着加快” | `3.8` |
| `device` | 推理设备；只测试过 `cpu` | `cpu` |
| `asr_dir` | 识别模型目录 | `tools/asr/models` |
| `cache_dir` | 缓存目录 | `.cache` |
| `port` | 页面端口；远程声卡通道用 `port + 1` | `9881` |
| `remote` | 远程电脑默认值，`{"target": "用户名@地址", "device": "BlackHole 2ch"}` | |

## 运行

```bash
./start.sh
```

浏览器打开 <http://127.0.0.1:9881/>，点“开始监听”。首次启动要为参考录音建立语气索引，需要几分钟。

### 可选：参考录音体检

不同参考录音的合成稳定性差别很大。服务运行时另开一个终端：

```bash
python scripts/audit_refs.py   # 用与服务相同的 Python 环境
```

它让每条参考合成 3 个句子再识别回来，之后实时页只列出稳定的参考。耗时较长，可以中断后续跑。

## 输出到会议软件

虚拟声卡有一个输出端和一个直通的输入端：VoxRelay 往输出端播放，会议软件把输入端当麦克风读取。推荐 [BlackHole](https://github.com/ExistentialAudio/BlackHole)：

```bash
brew install blackhole-2ch
```

### 本机虚拟声卡

页面左下“声音送到”选 **本机虚拟声卡**，选 BlackHole；会议软件的麦克风也选它。

### 内网另一台 Mac

开会的电脑需要：开启“系统设置 → 通用 → 共享 → 远程登录”，并装好虚拟声卡。本机需要能免密登录它：

```bash
ssh-copy-id 用户名@内网地址
./start.sh check 用户名@内网地址     # 逐步检查登录、拷贝程序、声卡、播放
./start.sh
```

页面上“声音送到”选 **内网另一台 Mac**，填地址，点“开始监听”。对方会议软件的麦克风选虚拟声卡，**扬声器不要选它**。

链路：浏览器 →（本机 WebSocket）→ 服务 →（SSH）→ 对方的播放程序 → 虚拟声卡。第一次连接会把约 200 KB 的播放程序拷到对方的 `~/.voxrelay/`。程序只改变自己的输出设备，不动对方的系统默认声卡；找不到指定设备时直接报错，不会改用扬声器。内网上不需要新开端口。

## 使用提示

| 设置 | 作用 |
|---|---|
| 多短的停顿就切段 / 最长一段 | 越短越快出声，识别错误略多 |
| 停顿多久算说完 | 判定一句话结束 |
| 语气保持时间 | 间隔不超过它的话沿用同一参考、种子和语速 |
| 我戴着耳机 | 用扬声器外放时关闭：说完才播，播放期间暂停拾音，避免录到自己。输出到虚拟声卡或远程电脑时不需要 |

会议用的电脑如果外放且离你的麦克风很近，别人说的话可能被识别后发回会议，请戴耳机。

## 实测数据

Mac mini（M4，CPU 推理），一个 v2Pro 音色，用训练录音模拟麦克风输入。换机器和音色结果会不同。

| 指标 | 数值 |
|---|---|
| 单段识别 | 约 0.45–0.8 s |
| 流式合成首包 | 约 0.65–1 s |
| 每段“段尾到出声” | 约 1.6–2.9 s，排队时可到 4 s |
| 连续说话时首次出声 | 开口后约 5.5–6 s |
| 长句错字率（整句 → 切段） | 2.4% → 3.6% |
| 段间音高波动（随机参考 → 共用参考） | 0.80 → 0.34 半音 |
| 说话人相似度（随机参考 → 共用参考） | 0.731 → 0.863 |

## 已知限制

- 远程声卡链路只用模拟的 `ssh` 测过协议和页面流程，**尚未在真实的第二台机器上验证**出声和延迟。
- 本机虚拟声卡输出尚未实机验证；断句参数尚未用多种真实麦克风和噪声环境验证。
- 强制切段可能切在词中间。
- 开启“边说边合”时，只要最后一段语音超过约 1.5 秒，它会在短停顿处提前发出，按“还没说完”的语调（逗号）合成；很短的句子才是正常的句末语调。关闭该开关可避免，但要等整句说完才出声。
- 快捷键只在页面处于前台时有效。
- 远程目标只支持 macOS。

## 安全

服务只监听 `127.0.0.1`，没有登录机制。HTTP 请求校验 `Host` 头，远程声卡通道只接受本页面来源的 WebSocket 连接，SSH 地址按固定格式校验。请不要用反向代理把它暴露到网络上。

## 测试

```bash
python tests/test_relay.py        # 远程通道（假 ssh，不需要模型和声卡）
node tests/test_segmenter.mjs     # 断句器
```

## 使用须知

请只使用你本人的声音，或已获得明确授权的声音。不要用它冒充他人、欺骗或误导；在会议或通话中使用合成声音时，请按当地法律和场合要求告知对方。

## 致谢与许可

- [GPT-SoVITS](https://github.com/RVC-Boss/GPT-SoVITS)（MIT）：语音合成。`voxrelay/voice_convert.py` 调用其内部接口。
- [FunASR](https://github.com/modelscope/FunASR) Paraformer：语音识别，模型遵循其各自许可。
- [BlackHole](https://github.com/ExistentialAudio/BlackHole)：虚拟声卡，需单独安装，本仓库不包含。

本仓库代码以 [MIT](LICENSE) 许可发布。仓库不包含任何模型权重或录音。
