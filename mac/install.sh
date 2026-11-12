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
codesign --force --deep -s - "$app"

echo "== 步骤 4/4：启动 =="
open "$app"

echo "完成。按 ⌥⇧K 打开面板；第一次需要在面板里登录一次。"
echo "把以上全部输出发给 Claude"
