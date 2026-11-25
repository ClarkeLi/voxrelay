# VoxRelay 工作流

## 新会话快速上手

- 启动：`./start.sh`（要用远程声卡时在系统“终端”里跑），开 http://127.0.0.1:9881/ 。配置见 `config.example.json` 与 README。
- 结构：`voxrelay/server.py`（HTTP 服务、识别、合成、参考匹配）、`voxrelay/relay.py`（远程声卡通道）、`voxrelay/config.py`；`web/`（页面，`live.js` 实时页、`audio.js` 断句与播放队列、`output.js` 输出去向、`tune.js` 精调页）；`remote/vox_play.swift`（对方机器上的播放程序）。
- 测试：`python tests/test_relay.py`、`node tests/test_segmenter.mjs`。
- 本机私有信息（路径、内网地址）记在 `WORKFLOW.local.md`，不进 git。

## 状态日志

- 2026-11-25：从实验项目整理为独立仓库 VoxRelay，本机路径与音色相关内容改为配置项；增加 HTTP `Host` 头校验；参考缓存按参考列表分目录。
  - 已知问题：分段开启时，超过约 1.5 s 的尾段按非句末（逗号语调）合成（`web/audio.js` 短停顿切段先于说完判定）。
  - 未验证：真实的第二台机器（SSH、拷程序、出声、延迟、断线重连）；本机虚拟声卡实机输出；多种真实麦克风与噪声环境。
- 2026-11-25：README 改版（徽章、流程图、实时页截图 `docs/screenshot.jpg`），加入与 RVC 的客观对比（`scripts/compare_metrics.py`、`scripts/plot_comparison.py`、`docs/comparison*`）。
  - 结果：同源小数据下 VoxRelay 可懂度（CER 0.29，RVC 实时 0.44）与音色相似度更好；RVC 的语调相关（0.86 对 0.35）、时长保留、延迟明显更好。无主观听感 / MOS。
  - 评测方法教训：语调相关要先按频谱 DTW 对齐再比逐帧音高；把有声区间拉伸到固定点数的算法会把对齐的输出也算成低相关。

