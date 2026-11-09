#!/usr/bin/env bash
# 一键启动 session-sync GUI：macOS 可在 Finder 双击，或终端运行 ./start-gui.command
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$DIR"
echo "启动 session-sync GUI（停止：Ctrl+C 或页面左侧栏底部“退出”）"
exec "$DIR/bin/session-sync" ui "$@"