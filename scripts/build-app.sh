#!/usr/bin/env bash
# 打包 macOS 应用：dist/Session Sync.app（自包含 Python，双击即用）
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

find_python() {
  local cand
  for cand in /opt/homebrew/bin/python3 /usr/local/bin/python3 python3; do
    command -v "$cand" >/dev/null 2>&1 || continue
    if "$cand" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)' 2>/dev/null; then
      command -v "$cand"
      return 0
    fi
  done
  return 1
}

PY="${PYTHON:-}"
if [ -z "$PY" ]; then
  PY="$(find_python || true)"
fi
if [ -z "$PY" ]; then
  echo "error: 需要 Python >= 3.10 来构建（例如 brew install python@3.13；也可用 PYTHON=/path/to/python3 指定）" >&2
  exit 1
fi
if ! "$PY" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)' 2>/dev/null; then
  echo "error: $PY 不是 Python >= 3.10" >&2
  exit 1
fi

VENV="$ROOT/.venv-packaging"
if [ ! -x "$VENV/bin/python" ]; then
  "$PY" -m venv "$VENV"
fi
if ! "$VENV/bin/python" -c "import PyInstaller" >/dev/null 2>&1; then
  echo "安装 PyInstaller（仅构建期依赖）..."
  "$VENV/bin/python" -m pip install --quiet --upgrade pip
  "$VENV/bin/python" -m pip install --quiet "pyinstaller>=6.22"
fi

# AppIcon.png -> build/AppIcon.icns
ICON_SRC="$ROOT/packaging/AppIcon.png"
ICONSET="$ROOT/build/AppIcon.iconset"
if [ -f "$ICON_SRC" ]; then
  rm -rf "$ICONSET"
  mkdir -p "$ICONSET"
  for entry in "16 icon_16x16" "32 icon_16x16@2x" "32 icon_32x32" "64 icon_32x32@2x" \
               "128 icon_128x128" "256 icon_128x128@2x" "256 icon_256x256" \
               "512 icon_256x256@2x" "512 icon_512x512" "1024 icon_512x512@2x"; do
    set -- $entry
    sips -z "$1" "$1" "$ICON_SRC" --out "$ICONSET/$2.png" >/dev/null
  done
  iconutil -c icns "$ICONSET" -o "$ROOT/build/AppIcon.icns"
fi

"$VENV/bin/python" -m PyInstaller --noconfirm --clean \
  --distpath "$ROOT/dist" --workpath "$ROOT/build/pyinstaller" \
  "$ROOT/packaging/session-sync.spec"

APP="$ROOT/dist/Session Sync.app"
if command -v codesign >/dev/null 2>&1; then
  codesign --force --deep --sign - "$APP" >/dev/null 2>&1 \
    || echo "warn: ad-hoc 签名失败（本地运行通常不受影响）" >&2
fi

echo "✓ 已生成: $APP"
