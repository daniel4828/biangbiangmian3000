#!/bin/bash
# Baut die Menüleisten-App (Swift-Package in mac/) und legt das Programm unter mac/.build/ ab.
set -euo pipefail

cd "$(dirname "$0")"
if [ -d /Applications/Xcode.app/Contents/Developer ]; then
  export DEVELOPER_DIR=/Applications/Xcode.app/Contents/Developer
fi
swift build -c release --product biangbiang-quick >&2
bin="$(swift build -c release --show-bin-path)"
# Fester Pfad für die Installation.
cp "$bin/biangbiang-quick" .build/biangbiang-quick
