#!/bin/zsh
# 双击运行：确认状态服务在跑，然后实时显示宠物此刻感知到的状态。
# 你可以一边让 WorkBuddy 干活，一边看 state 怎么变 —— 这就是"联动"的直接证据。
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
[ -z "$PY" ] && { echo "没找到 python3"; read -k 1; exit 1; }

if ! /usr/bin/curl --noproxy "*" -s -m 1 http://127.0.0.1:8791/health >/dev/null 2>&1; then
  echo "状态服务没在跑，正在拉起…"
  nohup "$PY" pet_daemon.py --port 8791 >/tmp/workbuddy-pet.log 2>&1 &!
  sleep 2
fi

exec "$PY" pet_watch.py
