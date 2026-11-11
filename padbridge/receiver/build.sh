#!/bin/bash
# Builds PadBridge.app into build/, ad-hoc signed. Usage: ./build.sh [--run]
set -euo pipefail
cd "$(dirname "$0")"
ROOT="$PWD"
APP="$ROOT/build/PadBridge.app"

swift build -c release --arch arm64
BIN="$(swift build -c release --arch arm64 --show-bin-path)/PadBridge"

rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
cp "$BIN" "$APP/Contents/MacOS/PadBridge"
cp Resources/Info.plist "$APP/Contents/Info.plist"

# App icon, drawn by Tools/make-icon.swift
ICONWORK="$ROOT/build/icon"
rm -rf "$ICONWORK"; mkdir -p "$ICONWORK/AppIcon.iconset"
swift Tools/make-icon.swift "$ICONWORK/icon_1024.png"
for s in 16 32 128 256 512; do
  sips -z $s $s "$ICONWORK/icon_1024.png" --out "$ICONWORK/AppIcon.iconset/icon_${s}x${s}.png" >/dev/null
  d=$((s * 2))
  sips -z $d $d "$ICONWORK/icon_1024.png" --out "$ICONWORK/AppIcon.iconset/icon_${s}x${s}@2x.png" >/dev/null
done
iconutil -c icns "$ICONWORK/AppIcon.iconset" -o "$APP/Contents/Resources/AppIcon.icns"

codesign --force --sign - --timestamp=none "$APP"
codesign --verify --verbose=1 "$APP"
echo "Built $APP"

if [[ "${1:-}" == "--run" ]]; then open "$APP"; fi
