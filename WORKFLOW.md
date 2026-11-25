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
