#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
WorkBuddy Pet —— 本地状态服务（纯 Python 标准库）

职责：把「Agent 现在在干什么」翻译成一个小 HTTP 接口，供宠物读取。
**本身不含任何工具专属逻辑** —— 那些都在 pet_sources.py 的适配器里，
所以换工具不用改运行时，别人也能接自己的。

    GET /state     → {"agent": {state, label, task, cwd, ...}, "source": ...}
    GET /usage     → 用量数据（只有 WorkBuddy 源才有）
    GET /health    → {"ok": true}
    GET /          → 用量报告.html
    GET /pet.html  → 桌面宠物页面
    GET /assets/*  → 素材

用法：
    python3 pet_daemon.py                # 自动挑选状态源
    python3 pet_daemon.py --source none  # 独立模式（宠物只做 idle 动画）
    python3 pet_daemon.py --source codex
    python3 pet_daemon.py --list         # 看本机有哪些状态源可用
    python3 pet_daemon.py --once         # 打印一次状态（调试）
"""
import argparse
import datetime as dt
import importlib
import json
import os
import socketserver
import sys
import threading
import time
import webbrowser
from http.server import SimpleHTTPRequestHandler
from urllib.parse import urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import pet_sources as SRC          # noqa: E402
import usage_stats as US           # noqa: E402

PORT_FILE = os.path.join(HERE, "pet.port")
DEFAULT_PORT = 8791
USAGE_TTL = 20.0

_source = None                      # 当前状态源实例（main 里设置）


# ---------------------------------------------------------------- 用量（WorkBuddy 专属）

_usage_lock = threading.Lock()
_usage_cache = {"ts": 0.0, "data": None}


def compute_usage():
    now = dt.datetime.now()
    start = dt.datetime(now.year, now.month, 1)
    records = [r for r in US.iter_calls()
               if US.is_deepseek(r["model"]) and r["when"] >= start]
    agg = US.aggregate(records)
    return {
        "scope": "%d 年 %d 月" % (now.year, now.month),
        "inputTokens": agg["prompt"], "outputTokens": agg["completion"],
        "totalTokens": agg["total"], "cachedTokens": agg["cached"],
        "calls": agg["calls"], "activeDays": agg["active_days"],
        "byModel": {m: v for m, v in agg["by_model"].most_common()},
    }


def usage_snapshot():
    """按需计算 + TTL 缓存（不后台预热：守护进程长期驻留，全量扫盘很浪费）。

    非 WorkBuddy 环境下直接返回 None —— 别的工具没有这套记录。
    """
    if not os.path.isdir(SRC.PROJECTS_DIR):
        return None
    with _usage_lock:
        if _usage_cache["data"] is not None and time.time() - _usage_cache["ts"] < USAGE_TTL:
            return _usage_cache["data"]
        try:
            data = compute_usage()
        except Exception as exc:
            data = {"error": str(exc)}
        _usage_cache["data"] = data
        _usage_cache["ts"] = time.time()
        return data


# ---------------------------------------------------------------- 重新统计

_regen_lock = threading.Lock()


def regenerate_report():
    """重新扫描本地记录、重写用量报告.html。

    报告页是**生成那一刻的快照**，不重写就永远停在那儿 ——
    用户盯着上午 10:50 的文件看了一整天，会直接认为"统计坏了"。
    所以页面上的「重新统计」按钮打到这里。

    用非阻塞锁：扫盘要几秒，连点两次不该排两次队。
    """
    if not _regen_lock.acquire(blocking=False):
        return {"ok": False, "error": "已经有一次重新统计在进行中，稍等一下"}
    try:
        # ⚠️ **必须 reload。** 这是个长期驻留的进程，import 一次就把 usage_stats
        # 载进内存了；之后改了那个文件、服务仍然在跑旧代码 ——
        # 症状是用户点了「重新统计」，页面又变回旧样子
        #（真实踩过：黄底提示条已经删掉，点一次它又回来了）。
        importlib.reload(US)
        agg, title, path, _meta = US.generate()
        _usage_cache["ts"] = 0.0                 # 顺手让 /usage 的缓存失效
        return {"ok": True, "generatedAt": int(time.time() * 1000),
                "totalTokens": agg["total"], "calls": agg["calls"],
                "title": title, "path": path}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
    finally:
        _regen_lock.release()


# ---------------------------------------------------------------- 状态组装

def build_state():
    now = int(time.time() * 1000)
    if _source is None:
        agent = SRC.blank("offline")
        sname, stitle = "none", "未初始化"
    else:
        agent = _source.snapshot()
        sname, stitle = _source.name, _source.title
    return {
        "ok": True,
        "ts": now,
        "apiVersion": 3,
        "source": sname,
        "sourceTitle": stitle,
        "agent": agent,
    }


def build_usage():
    now = int(time.time() * 1000)
    snap = usage_snapshot()
    if snap is None:
        return {"ok": True, "ts": now, "apiVersion": 1, "usage": None,
                "note": "当前环境没有用量数据（只有 WorkBuddy 源有）"}
    return {"ok": True, "ts": now, "apiVersion": 1, "usage": snap}


# ---------------------------------------------------------------- HTTP

class PetHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=HERE, **kwargs)

    def log_message(self, fmt, *args):
        pass

    def end_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def _json(self, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = urlparse(self.path).path
        if path in ("/state", "/api/state"):
            return self._json(build_state())
        if path in ("/usage", "/api/usage"):
            return self._json(build_usage())
        if path in ("/regenerate", "/api/regenerate"):
            return self._json(regenerate_report())
        if path in ("/health", "/version"):
            return self._json({"ok": True, "version": "pet-3.0",
                               "source": _source.name if _source else None})
        if path == "/":
            self.path = "/" + "用量报告.html"
        return super().do_GET()


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def serve(port, open_browser):
    httpd = None
    for p in range(port, port + 12):
        try:
            httpd = Server(("127.0.0.1", p), PetHandler)
            port = p
            break
        except OSError:
            continue
    if httpd is None:
        print("端口都被占用了，换一个 --port 试试")
        return 1
    try:
        with open(PORT_FILE, "w", encoding="utf-8") as fh:
            fh.write(str(port))
    except OSError:
        pass
    url = "http://127.0.0.1:%d/" % port
    print("WorkBuddy Pet 状态服务已启动")
    print("  状态源: %s（%s）" % (_source.name, _source.title))
    print("  页面:   %s" % url)
    print("  状态:   %sstate" % url)
    print("  按 Ctrl+C 停止")
    if open_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
    finally:
        httpd.server_close()
        try:
            os.remove(PORT_FILE)
        except OSError:
            pass
    return 0


def main():
    global _source
    ap = argparse.ArgumentParser(description="WorkBuddy Pet 状态服务")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--source", default="auto",
                    help="状态源：auto（默认）/ workbuddy / codex / none")
    ap.add_argument("--open", action="store_true", help="启动后打开页面")
    ap.add_argument("--once", action="store_true", help="只打印一次状态（调试）")
    ap.add_argument("--list", action="store_true", help="列出本机可用的状态源")
    args = ap.parse_args()

    if args.list:
        print("本机可用的状态源：")
        for key, title, avail in SRC.describe():
            print("  %-10s %-26s %s" % (key, title, "可用" if avail else "没找到数据"))
        print("\n用 --source <名字> 指定；默认 auto 会挑此刻真的有活动的那个。")
        return 0

    _source = SRC.pick(args.source)
    if args.source == "auto" and not args.once:
        # --once 时不能往 stdout 打这句，否则会污染 JSON 输出
        print("自动选择状态源：%s（%s）" % (_source.name, _source.title))
    elif args.source == "auto":
        print("自动选择状态源：%s" % _source.name, file=sys.stderr)

    if args.once:
        print(json.dumps(build_state(), ensure_ascii=False, indent=2))
        return 0

    return serve(args.port, args.open)


if __name__ == "__main__":
    sys.exit(main())
