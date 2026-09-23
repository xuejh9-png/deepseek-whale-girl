#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
桌宠状态实时监视 —— 每秒打印一次「宠物此刻感知到的 Agent 状态」。

用途：验证"宠物是否真的和任务状态联动"。
      一边让 WorkBuddy 干活，一边看这里的 state 怎么变。

    python3 pet_watch.py            # 实时刷新
    python3 pet_watch.py --once     # 只打印一次
    python3 pet_watch.py --port N
"""
import argparse
import json
import sys
import time
import urllib.request

COLOR = {
    "working":  "\033[32m",   # 绿
    "thinking": "\033[36m",   # 青
    "success":  "\033[32m",
    "waiting":  "\033[33m",   # 黄
    "error":    "\033[31m",   # 红
    "idle":     "\033[90m",   # 灰
    "offline":  "\033[31m",
}
RESET = "\033[0m"

# 每个状态在宠物身上对应播哪个剪辑（和 pet-runtime.js 的 STATE_CLIP 一致）
CLIP = {
    "idle": "idle", "thinking": "think", "working": "work",
    "waiting": "idle", "success": "success",
    "error": "error", "sleeping": "sleep", "offline": "idle",
}


def fetch(port):
    url = f"http://127.0.0.1:{port}/state"
    with urllib.request.urlopen(url, timeout=2) as r:
        return json.load(r)


def line(port):
    try:
        d = fetch(port)
    except Exception:
        return f"  {COLOR['offline']}{'连不上':<12}{RESET} 状态服务没在跑（双击「启动桌面宠物.command」）"
    a = d.get("agent", {})
    s = a.get("state", "?")
    c = COLOR.get(s, "")
    task = (a.get("task") or "")[:46]
    return (f"  {time.strftime('%H:%M:%S')}  {c}{s:<10}{RESET}"
            f"{('→ ' + CLIP.get(s, '?')):<12}{task}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8791)
    ap.add_argument("--once", action="store_true")
    args = ap.parse_args()

    if args.once:
        print(line(args.port))
        return 0

    print()
    print("  桌宠状态实时监视    （Ctrl+C 退出）")
    print("  ────────────────────────────────────────────────────────────────")
    print("  时间        state       宠物在播      当前任务（我读到的）")
    print("  ────────────────────────────────────────────────────────────────")
    try:
        while True:
            print(line(args.port), flush=True)
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n  已退出")
    return 0


if __name__ == "__main__":
    sys.exit(main())
