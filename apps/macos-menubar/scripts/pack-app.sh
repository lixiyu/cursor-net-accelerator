#!/usr/bin/env bash
# 打出 CursorNetAccelerator.app（自用，不签名）
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
REPO="$(cd "$ROOT/../.." && pwd)"
# 构建产物可用英文路径；安装到 /Applications 时用中文包名（Finder 列表看的是文件夹名）
OUT="${1:-$ROOT/dist/CursorNetAccelerator.app}"
INSTALL_NAME="${CURSOR_NET_ACCEL_INSTALL_NAME:-Cursor 网络加速器.app}"
BIN_ACCEL="${CURSOR_NET_ACCEL_BIN:-$REPO/.venv/bin/cursor-net-accelerator}"

cd "$ROOT"
swift build -c release
EXE="$(swift build -c release --show-bin-path)/CursorNetAccelMenu"

rm -rf "$OUT"
mkdir -p "$OUT/Contents/MacOS" "$OUT/Contents/Resources"
cp "$EXE" "$OUT/Contents/MacOS/CursorNetAccelerator"
cp "$ROOT/Resources/Info.plist" "$OUT/Contents/Info.plist"
if [[ -f "$ROOT/Resources/AppIcon.icns" ]]; then
  cp "$ROOT/Resources/AppIcon.icns" "$OUT/Contents/Resources/AppIcon.icns"
fi
for f in MenuBarIcon.png "MenuBarIcon@2x.png" "MenuBarIcon@3x.png"; do
  if [[ -f "$ROOT/Resources/$f" ]]; then
    cp "$ROOT/Resources/$f" "$OUT/Contents/Resources/$f"
  fi
done

EXAMPLE="$OUT/Contents/Resources/menubar.example.json"
printf '{\n  "bin": "%s"\n}\n' "$BIN_ACCEL" >"$EXAMPLE"

echo "已生成: $OUT"
if [[ "${CURSOR_NET_ACCEL_INSTALL:-}" == "1" ]]; then
  DEST="/Applications/$INSTALL_NAME"
  rm -rf "$DEST" "/Applications/CursorNetAccelerator.app"
  cp -R "$OUT" "$DEST"
  xattr -cr "$DEST" 2>/dev/null || true
  echo "已安装: $DEST"
fi
