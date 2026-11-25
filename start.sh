#!/usr/bin/env bash
# 启动 VoxRelay。要用远程声卡时请在系统“终端”里运行（需要 SSH）。
#   ./start.sh                                      启动服务，Ctrl-C 结束
#   ./start.sh check [用户名@地址] [--device 名称]  检查能否把声音送到另一台 Mac
cd "$(dirname "$0")" || exit 1
[ -f local.env ] && . ./local.env # 本机专用设置（VOXRELAY_PYTHON、TMPDIR 等），不进 git
PY=${VOXRELAY_PYTHON:-python3}
export PYTHONPATH="$PWD${PYTHONPATH:+:$PYTHONPATH}"
if [ "$1" = check ]; then
  shift
  "$PY" -u -m voxrelay check "$@" 2>&1 | tee "${TMPDIR:-/tmp}/voxrelay_check.log"
  exit "${PIPESTATUS[0]}"
fi
pkill -f "m voxrelay serve" && sleep 2 # 停掉上一次没关的服务
exec "$PY" -u -m voxrelay serve "$@"
