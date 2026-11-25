# 约定与坑

- `config.json`、`local.env`、`WORKFLOW.local.md` 是本机私有内容，已在 `.gitignore`；仓库里不放内网地址、本机路径、录音和模型。
- Python 代码放在 `voxrelay/` 包内：服务会把 GPT-SoVITS 目录加进 `sys.path`，顶层模块名（如 `config`、`tools`）会和它冲突。
- 服务启动时 `chdir` 到 GPT-SoVITS 目录；本仓库内的文件用 `config.HOME` 定位。
- 只监听 `127.0.0.1`。`Host` 头校验、WebSocket 的 Origin 限制、SSH 地址正则和 `shlex.quote` 不要去掉。
- `voxrelay/voice_convert.py` 依赖 GPT-SoVITS 的内部接口，升级 GPT-SoVITS 后要检查。
- 改前端无需重启服务；改 Python 需要重启。
- 浏览器自测时用假麦克风，并避免改写用户的 `localStorage`；假 `ssh` 用环境变量 `VOXRELAY_SSH` 注入，测完停掉那个服务。
- 提交前运行 `python tests/test_relay.py` 和 `node tests/test_segmenter.mjs`。
