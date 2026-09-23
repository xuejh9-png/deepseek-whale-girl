#!/bin/zsh
# 双击运行：确认状态服务在跑，然后实时显示宠物此刻感知到的状态。
# 你可以一边让 WorkBuddy 干活，一边看 state 怎么变 —— 这就是"联动"的直接证据。
cd "$(dirname "$0")" || exit 1

PY=""
for c in /Users/c0437/.local/bin/python3 \
         /Users/c0437/.workbuddy/binaries/python/versions/3.13.12/bin/python3 \
         /opt/homebrew/bin/python3 /usr/bin/python3; do
  [ -x "$c" ] && PY="$c" && break
done
[ -z "$PY" ] && { echo "没找到 python3"; read -k 1; exit 1; }

if ! /usr/bin/curl -s -m 1 http://127.0.0.1:8791/health >/dev/null 2>&1; then
  echo "状态服务没在跑，正在拉起…"
  nohup "$PY" pet_daemon.py --port 8791 >/tmp/workbuddy-pet.log 2>&1 &!
  sleep 2
fi

exec "$PY" pet_watch.py
