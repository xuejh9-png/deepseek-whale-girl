#!/bin/zsh
# 编译桌面宠物宿主（Swift + WKWebView）
# 产物：desktop/WorkBuddyPet.app
set -e

HERE="$(cd "$(dirname "$0")" && pwd)"
APP="$HERE/WorkBuddyPet.app"

if ! command -v swiftc >/dev/null 2>&1; then
  echo "找不到 swiftc。请先装命令行工具：xcode-select --install"
  exit 1
fi

echo "编译桌面宠物…"
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"

cat > "$APP/Contents/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleName</key><string>WorkBuddyPet</string>
  <key>CFBundleDisplayName</key><string>WorkBuddy 宠物</string>
  <key>CFBundleIdentifier</key><string>local.workbuddy.pet</string>
  <key>CFBundleExecutable</key><string>WorkBuddyPet</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>1.0</string>
  <key>CFBundleVersion</key><string>1</string>
  <key>LSMinimumSystemVersion</key><string>11.0</string>
  <key>NSHighResolutionCapable</key><true/>
  <key>LSUIElement</key><true/>
</dict>
</plist>
PLIST

swiftc -O \
  -o "$APP/Contents/MacOS/WorkBuddyPet" \
  "$HERE/main.swift" \
  -framework Cocoa -framework WebKit

chmod +x "$APP/Contents/MacOS/WorkBuddyPet"
echo "✓ 编译完成：$APP"
echo "  启动：双击 desktop/WorkBuddyPet.app，或跑 启动桌面宠物.command"
