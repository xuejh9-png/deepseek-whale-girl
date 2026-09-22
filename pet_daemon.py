#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
WorkBuddy Pet —— 本地状态服务（纯 Python 标准库）

职责：只读 WorkBuddy 的本地记录，推断 Agent 当前状态 + 汇总真实 token 用量，
通过 HTTP 暴露给网页宠物 / 桌面宠物。

    GET /state     → JSON 状态
    GET /health    → {"ok": true}
    GET /          → 用量报告.html
    GET /assets/*  → 图片等静态资源

数据来源（全部真实，不造假；取不到就返回 null）：
    ~/.workbuddy/projects/<项目>/<会话>.jsonl   ← 每次调用的 token 明细 + 活动序列
    ~/.workbuddy/sessions/*.json                ← 活跃会话心跳
    ~/.workbuddy/cache/acc-product-config-v3.json ← 模型上下文窗口

用法：
    python3 pet_daemon.py                # 启动（默认 8791 端口）
    python3 pet_daemon.py --open         # 启动并打开页面
    python3 pet_daemon.py --once         # 只打印一次状态，不起服务（调试用）
"""

import argparse
import datetime as dt
import glob
import json
import os
import re
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
import usage_stats as US  # noqa: E402  复用 token 解析逻辑，避免重复实现

WB = os.path.expanduser("~/.workbuddy")
PROJECTS_DIR = os.path.join(WB, "projects")
SESSIONS_DIR = os.path.join(WB, "sessions")
MODEL_CFG = os.path.join(WB, "cache", "acc-product-config-v3.json")
PORT_FILE = os.path.join(HERE, "pet.port")

DEFAULT_PORT = 8791
FRESH_MS = 4000           # 4 秒内 = 正在活动
WORK_WINDOW_MS = 25000    # 工具调用后多久还算"执行中"
SUCCESS_WINDOW_MS = 25000 # 回复后多久还算"刚完成"
LOOKBACK_MS = 180000      # 只回溯 3 分钟的记录
HEARTBEAT_ALIVE_MS = 60000
USAGE_TTL = 20.0          # 用量聚合缓存秒数
TAIL_BYTES = 900_000      # 只读 jsonl 尾部这么多字节

LABELS = {
    "idle": "空闲",
    "thinking": "思考中",
    "working": "执行中",
    "success": "刚完成",
    "waiting": "等你回复",
    "error": "出错了",
    "offline": "WorkBuddy 未运行",
    "sleeping": "睡着啦",
}


# ---------------------------------------------------------------- 数据采集

def active_sessions(now_ms):
    """返回心跳仍新鲜的会话（说明 WorkBuddy 在跑）。"""
    out = []
    for p in glob.glob(os.path.join(SESSIONS_DIR, "*.json")):
        try:
            with open(p, "r", encoding="utf-8") as fh:
                d = json.load(fh)
        except Exception:
            continue
        hb = d.get("lastHeartbeat") or 0
        if hb and now_ms - hb < HEARTBEAT_ALIVE_MS:
            out.append(d)
    return out


def latest_session():
    """挑出"最近真的有 LLM 活动"的会话，返回 (path, recs)。

    不能只看文件 mtime —— 后台任务通知之类的写入也会刷新 mtime，
    会把别的会话顶上来。所以比较各候选文件里最后一条记录的时间戳。
    多会话并发时先按 mtime 取前 4 个候选（省得全读），再比时间戳。
    """
    cands = []
    for p in glob.glob(os.path.join(PROJECTS_DIR, "*", "*.jsonl")):
        try:
            cands.append((os.path.getmtime(p), p))
        except OSError:
            pass
    if not cands:
        return None, []
    cands.sort(reverse=True)

    # 快速路径：最新文件刚被写过，基本就是它
    if time.time() - cands[0][0] < 5:
        return cands[0][1], read_tail(cands[0][1])

    best_p, best_recs, best_ts = cands[0][1], [], 0
    for _, p in cands[:4]:
        recs = read_tail(p, 50)
        ts = 0
        for r in reversed(recs):
            if r.get("timestamp"):
                ts = r["timestamp"]
                break
        if ts > best_ts:
            best_p, best_recs, best_ts = p, recs, ts
    if not best_recs:
        best_recs = read_tail(best_p)
    return best_p, best_recs


def read_tail(path, limit=320):
    """只读取文件尾部，避免把上百 MB 的会话全读进来。"""
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as fh:
            if size > TAIL_BYTES:
                fh.seek(-TAIL_BYTES, os.SEEK_END)
                fh.readline()          # 丢弃可能被截断的半行
            raw = fh.read().decode("utf-8", "ignore")
    except OSError:
        return []
    recs = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            recs.append(json.loads(line))
        except ValueError:
            continue
    return recs[-limit:]


def clean_text(txt):
    """去掉 system-reminder / 附件标记之类的噪音，留用户真正说的话。"""
    if not txt:
        return None
    txt = re.sub(r"<system-reminder.*?</system-reminder>", " ", txt, flags=re.S)
    txt = re.sub(r'@long-text:"[^"]*"', " ", txt)
    txt = re.sub(r"@image#\d+:\S+", " ", txt)
    txt = re.sub(r"@\S+\.(png|jpg|jpeg|gif|webp|pdf|docx|xlsx|txt|md)", " ", txt, flags=re.I)
    txt = re.sub(r"<[^>]{1,80}>", " ", txt)
    txt = re.sub(r"\s+", " ", txt).strip()
    return txt or None


def looks_like_noise(txt):
    """后台任务通知之类会以 user 身份注入，不该当成"用户的任务"。"""
    low = txt.lower()
    if "background command" in low or "system-reminder" in low:
        return True
    if "call_" in low and ("completed" in low or "started" in low or "failed" in low):
        return True
    if low.startswith("blocode") or "&quot;" in low:
        return True
    return len(txt) < 5


def scan_last_prompt(path, max_bytes=8_000_000):
    """从会话文件里找出最后一条"真正的用户输入"。

    长会话尾部可能几千条工具调用，所以不能只读尾部若干条。
    这里回扫较大一段，但先用字符串粗筛 role=user 的行，只对少量行做 JSON 解析。
    """
    if not path:
        return None
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as fh:
            if size > max_bytes:
                fh.seek(size - max_bytes)
                fh.readline()               # 丢掉半行
            raw = fh.read().decode("utf-8", "ignore")
    except OSError:
        return None

    best = None
    for line in raw.splitlines():
        if '"user"' not in line:
            continue
        try:
            r = json.loads(line)
        except ValueError:
            continue
        if r.get("type") != "message" or r.get("role") != "user":
            continue
        c = r.get("content")
        text = None
        if isinstance(c, list):
            for part in c:
                if isinstance(part, dict) and part.get("text"):
                    text = part["text"]
                    break
        elif isinstance(c, str):
            text = c
        got = clean_text(text)
        if got and not looks_like_noise(got):
            best = got                       # 继续往后扫 = 取最后一条
    return best[:140] if best else None


def context_usage(recs):
    """取该会话最后一次调用的 total_tokens —— 即当前上下文占用（真实值）。"""
    for r in reversed(recs):
        pd = r.get("providerData") or {}
        ru = pd.get("rawUsage") or {}
        total = ru.get("total_tokens") or 0
        if total:
            model = pd.get("requestModelName") or pd.get("model")
            return total, US.norm_model(model) if model else None
    return None, None


def model_context_window(model_name):
    if not model_name:
        return None
    try:
        with open(MODEL_CFG, "r", encoding="utf-8") as fh:
            cfg = json.load(fh)
    except Exception:
        return None
    models = cfg.get("models") or []
    target = str(model_name).lower()
    for m in models:
        if not isinstance(m, dict):
            continue
        names = {str(m.get(k, "")).lower() for k in
                 ("id", "model", "name", "displayName", "requestModelId")}
        if target in names or US.norm_model(m.get("id") or "") == US.norm_model(model_name):
            for key in ("contextWindow", "context_window", "maxInputTokens",
                        "max_input_tokens", "contextLength", "maxTokens"):
                v = m.get(key)
                if isinstance(v, int) and v > 0:
                    return v
    return None


def infer_state(now_ms, recs=None):
    """按 jsonl 尾部的记录序列推断 Agent 状态。"""
    if not active_sessions(now_ms):
        return "offline", {}
    if recs is None:
        path, recs = latest_session()
        if not path:
            return "idle", {}
    for r in reversed(recs):
        ts = r.get("timestamp") or 0
        if not ts:
            continue
        age = now_ms - ts
        if age > LOOKBACK_MS:
            break
        t = r.get("type")
        if t == "function_call":
            return ("working" if age < WORK_WINDOW_MS else "idle"), r
        if t == "function_call_result":
            return ("working" if age < FRESH_MS else "thinking"), r
        if t == "reasoning":
            if age < FRESH_MS:
                return "thinking", r
            continue
        if t == "message":
            if r.get("role") == "user":
                return ("thinking" if age < FRESH_MS else "waiting"), r
            return ("success" if age < SUCCESS_WINDOW_MS else "idle"), r
    return "idle", {}


# ---------------------------------------------------------------- 用量缓存

_usage_lock = threading.Lock()
_usage_cache = {"ts": 0.0, "data": None}


def compute_usage():
    """本月 DeepSeek 真实用量。全量扫描较慢，所以带缓存。"""
    now = dt.datetime.now()
    start = dt.datetime(now.year, now.month, 1)
    records = [r for r in US.iter_calls()
               if US.is_deepseek(r["model"]) and r["when"] >= start]
    agg = US.aggregate(records)
    return {
        "scope": "%d 年 %d 月" % (now.year, now.month),
        "inputTokens": agg["prompt"],
        "outputTokens": agg["completion"],
        "totalTokens": agg["total"],
        "cachedTokens": agg["cached"],
        "calls": agg["calls"],
        "activeDays": agg["active_days"],
        "byModel": {m: v for m, v in agg["by_model"].most_common()},
    }


def usage_snapshot():
    with _usage_lock:
        fresh = time.time() - _usage_cache["ts"] < USAGE_TTL
        if _usage_cache["data"] is not None and fresh:
            return _usage_cache["data"]
        try:
            data = compute_usage()
        except Exception as exc:            # 读不到就给 null，绝不编造
            data = {"error": str(exc)}
        _usage_cache["data"] = data
        _usage_cache["ts"] = time.time()
        return data


def usage_warmer():
    """后台预热，避免第一次请求卡住。"""
    while True:
        try:
            usage_snapshot()
        except Exception:
            pass
        time.sleep(USAGE_TTL)


# ---------------------------------------------------------------- 状态组装

def build_state():
    now = (time.time() * 1000)
    path, recs = latest_session()
    state, rec = infer_state(now, recs)

    ctx_tokens, model = context_usage(recs)
    window = model_context_window(model)

    # TokenUsage Adapter —— 统一格式，前端不依赖任何 provider 原始字段
    usage = {
        "inputTokens": None,
        "outputTokens": None,
        "totalTokens": None,
        "contextTokens": ctx_tokens,
        "contextWindow": window,
        "contextUsagePercent": (round(ctx_tokens / window * 100, 1)
                                if ctx_tokens and window else None),
        "model": model,
        "source": "workbuddy/projects jsonl",
    }
    snap = usage_snapshot() or {}
    if "totalTokens" in snap:
        usage["month"] = snap
        usage["inputTokens"] = snap.get("inputTokens")
        usage["outputTokens"] = snap.get("outputTokens")
        usage["totalTokens"] = snap.get("totalTokens")
        usage["cachedTokens"] = snap.get("cachedTokens")
        usage["calls"] = snap.get("calls")

    sessions = active_sessions(now)
    cur = None
    for s in sessions:
        if path and s.get("sessionId") and s["sessionId"] in path:
            cur = s
            break
    if cur is None and sessions:
        cur = sessions[0]

    return {
        "ok": True,
        "ts": int(now),
        "apiVersion": 1,
        "agent": {
            "state": state,
            "label": LABELS.get(state, state),
            "task": scan_last_prompt(path),
            "cwd": (cur or {}).get("cwd"),
            "sessionId": (cur or {}).get("sessionId"),
            "lastActivity": (rec or {}).get("timestamp"),
            "activeSessions": len(sessions),
        },
        "usage": usage,
    }


# ---------------------------------------------------------------- HTTP

class PetHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=HERE, **kwargs)

    def log_message(self, fmt, *args):      # 静音，别刷屏
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
        if path in ("/health", "/version"):
            return self._json({"ok": True, "version": "pet-1.0"})
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
    ap = argparse.ArgumentParser(description="WorkBuddy Pet 状态服务")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--open", action="store_true", help="启动后打开页面")
    ap.add_argument("--once", action="store_true", help="只打印一次状态（调试）")
    args = ap.parse_args()

    if args.once:
        print(json.dumps(build_state(), ensure_ascii=False, indent=2))
        return 0

    threading.Thread(target=usage_warmer, daemon=True).start()
    return serve(args.port, args.open)


if __name__ == "__main__":
    sys.exit(main())
