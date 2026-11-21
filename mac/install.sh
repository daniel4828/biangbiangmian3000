#!/bin/bash
# Baut und installiert "Biangbiang Quick" nach /Applications.
set -euo pipefail

cd "$(dirname "$0")"
app="/Applications/Biangbiang Quick.app"
bundle_id="eu.danielschreiber.biangbiang.quick"

echo "== 步骤 1/4：编译（首次约 1-2 分钟） =="
bash build.sh

echo "== 步骤 2/4：退出正在运行的旧版本 =="
osascript -e "tell application id \"$bundle_id\" to quit" >/dev/null 2>&1 || true
for _ in $(seq 1 20); do
  pgrep -x biangbiang-quick >/dev/null 2>&1 || break
  sleep 0.5
done

echo "== 步骤 3/4：组装并签名 ${app} =="
rm -rf "$app"
mkdir -p "$app/Contents/MacOS"
cp .build/biangbiang-quick "$app/Contents/MacOS/biangbiang-quick"
cp Support/Info.plist "$app/Contents/Info.plist"
mkdir -p "$app/Contents/Resources"
iconset="$(mktemp -d)/AppIcon.iconset"
mkdir -p "$iconset"
[ -d /Applications/Xcode.app/Contents/Developer ] && export DEVELOPER_DIR=/Applications/Xcode.app/Contents/Developer
swift Support/make_icon.swift "$iconset/icon_512x512@2x.png" >/dev/null
for s in 16 32 128 256 512; do
  sips -z $s $s "$iconset/icon_512x512@2x.png" --out "$iconset/icon_${s}x${s}.png" >/dev/null
  sips -z $((s*2)) $((s*2)) "$iconset/icon_512x512@2x.png" --out "$iconset/icon_${s}x${s}@2x.png" >/dev/null
done
iconutil -c icns "$iconset" -o "$app/Contents/Resources/AppIcon.icns"
codesign --force --deep -s - "$app"

touch "$app"
echo "== 步骤 4/4：启动 =="
open "$app"

echo "完成。按 ⌥⇧K 打开面板；第一次需要在面板里登录一次。"
echo "把以上全部输出发给 Claude"
