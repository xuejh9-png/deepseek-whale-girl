#!/bin/zsh
# 双击启动桌面宠物（会自动带上状态服务）
cd "$(dirname "$0")" || exit 1

# 找一个可用的 python3（相对路径优先，便于别人 clone 后直接跑）
PY=""
for c in "$HOME/.local/bin/python3" \
         "$HOME/.workbuddy/binaries/python/versions/3.13.12/bin/python3" \
         /opt/homebrew/bin/python3 \
         "$(command -v python3 2>/dev/null)" \
         /usr/bin/python3; do
  if [ -n "$c" ] && [ -x "$c" ]; then PY="$c"; break; fi
done
if [ -z "$PY" ]; then
  echo "没找到 python3，请先装命令行工具：xcode-select --install"
  read -k 1 "?按任意键退出..."
  exit 1
fi

CURL=/usr/bin/curl

echo ""
echo "  WorkBuddy 桌面宠物"
echo "  ────────────────────────────────"

# 1) 状态服务没在跑就拉起来
if $CURL -s -m 1 http://127.0.0.1:8791/health >/dev/null 2>&1; then
  echo "  状态服务：已在运行"
else
  echo "  状态服务：启动中…"
  nohup "$PY" pet_daemon.py --port 8791 >/tmp/workbuddy-pet.log 2>&1 &!
  sleep 2
  if $CURL -s -m 2 http://127.0.0.1:8791/health >/dev/null 2>&1; then
    echo "  状态服务：已就绪"
  else
    echo "  状态服务：启动失败，看 /tmp/workbuddy-pet.log"
  fi
fi

# 2) 没编译过就先编译
APP="desktop/WorkBuddyPet.app"
if [ ! -x "$APP/Contents/MacOS/WorkBuddyPet" ]; then
  echo "  首次运行，正在编译（约 10 秒）…"
  zsh desktop/build.sh >/tmp/workbuddy-pet-build.log 2>&1 || {
    echo "  编译失败，看 /tmp/workbuddy-pet-build.log"
    read -k 1 "?按任意键退出..."
    exit 1
  }
fi

# 3) 拉起宠物窗口
open "$APP"
echo "  宠物已启动"
echo ""
echo "  · 拖动她可以移动位置"
echo "  · 退出：按 ⌥⌘Q（Option+Command+Q）"
echo "    或者 右键点她 / 按住她 0.8 秒 唤出菜单"
echo ""
echo "  关掉工作区窗口后她也会继续待在桌面上。"
sleep 1
