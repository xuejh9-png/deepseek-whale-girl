#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
宠物状态源适配器 —— 「Agent 现在在干什么」从哪读。

运行时（网页 + 窗口）只认一个契约：GET /state 返回
    {"agent": {"state": ..., "label": ..., "task": ..., "cwd": ..., ...}}

状态名固定这几个（运行时的状态机按它们选剪辑）：
    idle / thinking / working / success / waiting / error / sleeping / offline

每个适配器只负责把「某个工具的本地记录」翻译成这个契约。
新增一个工具 = 新增一个类，运行时一行都不用改。

现有：
    WorkBuddySource  读 ~/.workbuddy/projects/*.jsonl + sessions/*.json
    CodexSource      读 ~/.codex/sessions/**/rollout-*.jsonl
    NullSource       永远 idle（没有状态源时，宠物只做自己的 idle 动画）
"""
import datetime as dt
import glob
import json
import os
import re
import time

WB = os.path.expanduser("~/.workbuddy")
PROJECTS_DIR = os.path.join(WB, "projects")
SESSIONS_DIR = os.path.join(WB, "sessions")
MODEL_CFG = os.path.join(WB, "cache", "acc-product-config-v3.json")

CODEX = os.path.expanduser("~/.codex")
CODEX_SESSIONS = os.path.join(CODEX, "sessions")

LABELS = {
    "idle": "空闲", "thinking": "思考中", "working": "执行中",
    "success": "刚完成", "waiting": "等你回复", "error": "出错了",
    "offline": "未连接", "sleeping": "睡着啦",
}

# 判定窗口（毫秒）
FRESH_MS = 4000            # 这么久内有活动 = 正在干活
WORK_WINDOW_MS = 25000     # 工具调用后多久还算「执行中」
SUCCESS_WINDOW_MS = 25000  # 回复后多久还算「刚完成」
WAITING_MS = 90000         # 静默多久算「空闲」


def blank(state="idle", label=None, **kw):
    d = {"state": state, "label": label or LABELS.get(state, state),
         "task": None, "cwd": None, "sessionId": None,
         "lastActivity": None, "activeSessions": 0}
    d.update(kw)
    return d


def now_ms():
    return int(time.time() * 1000)


# ---------------------------------------------------------------- 基类

class BaseSource:
    name = "base"
    title = "未命名"

    def available(self):
        """本机有没有这个工具的数据"""
        return True

    def snapshot(self):
        """返回契约 dict。读不到就返回 idle / offline。"""
        raise NotImplementedError

    def usage(self):
        """可选的用量信息；没有就返回 None"""
        return None


# ---------------------------------------------------------------- 无状态源

class NullSource(BaseSource):
    """没有任何 Agent 工具在跑 —— 宠物就做自己的 idle 动画。"""
    name = "none"
    title = "独立模式（无状态源）"

    def snapshot(self):
        return blank("idle", "独立模式", source=self.name)


# ---------------------------------------------------------------- WorkBuddy

class WorkBuddySource(BaseSource):
    name = "workbuddy"
    title = "WorkBuddy"

    def available(self):
        return os.path.isdir(PROJECTS_DIR)

    # ---- 读活跃会话 ----
    def _active_sessions(self, now):
        out = []
        for p in glob.glob(os.path.join(SESSIONS_DIR, "*.json")):
            try:
                with open(p, "r", encoding="utf-8") as fh:
                    d = json.load(fh)
            except Exception:
                continue
            hb = d.get("lastHeartbeat") or 0
            if hb and now - hb < 60000:
                out.append(d)
        return out

    def _read_tail(self, path, limit=320, bytes_=900_000):
        try:
            size = os.path.getsize(path)
            with open(path, "rb") as fh:
                if size > bytes_:
                    fh.seek(-bytes_, os.SEEK_END)
                    fh.readline()
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

    def _latest_session(self):
        cands = []
        for p in glob.glob(os.path.join(PROJECTS_DIR, "*", "*.jsonl")):
            try:
                cands.append((os.path.getmtime(p), p))
            except OSError:
                pass
        if not cands:
            return None, []
        cands.sort(reverse=True)
        if time.time() - cands[0][0] < 5:
            return cands[0][1], self._read_tail(cands[0][1])
        best_p, best_recs, best_ts = cands[0][1], [], 0
        for _, p in cands[:4]:
            recs = self._read_tail(p, 50)
            ts = 0
            for r in reversed(recs):
                if r.get("timestamp"):
                    ts = r["timestamp"]
                    break
            if ts > best_ts:
                best_p, best_recs, best_ts = p, recs, ts
        if not best_recs:
            best_recs = self._read_tail(best_p)
        return best_p, best_recs

    def _scan_last_prompt(self, path, max_bytes=8_000_000):
        if not path:
            return None
        try:
            size = os.path.getsize(path)
            with open(path, "rb") as fh:
                if size > max_bytes:
                    fh.seek(size - max_bytes)
                    fh.readline()
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
            got = _clean_text(text)
            if got and not _looks_like_noise(got):
                best = got
        return best[:140] if best else None

    def snapshot(self):
        now = now_ms()
        if not self._active_sessions(now):
            return blank("offline", **{"source": self.name})
        path, recs = self._latest_session()
        if not path:
            return blank("idle", source=self.name)

        state, rec = "idle", {}
        for r in reversed(recs):
            ts = r.get("timestamp") or 0
            if not ts:
                continue
            age = now - ts
            if age > 180000:
                break
            t = r.get("type")
            if t == "function_call":
                state = "working" if age < WORK_WINDOW_MS else "idle"
                rec = r
                break
            if t == "function_call_result":
                state = "working" if age < FRESH_MS else "thinking"
                rec = r
                break
            if t == "reasoning":
                if age < FRESH_MS:
                    state, rec = "thinking", r
                    break
                continue
            if t == "message":
                if r.get("role") == "user":
                    # 球在 Agent 这边 —— 它在生成，只是还没落盘。不该是 waiting。
                    state = "thinking" if age < WAITING_MS else "idle"
                else:
                    # Agent 把球交回给你了
                    if age < SUCCESS_WINDOW_MS:
                        state = "success"
                    elif age < WAITING_MS:
                        state = "waiting"
                    else:
                        state = "idle"
                rec = r
                break

        sessions = self._active_sessions(now)
        cur = None
        for s in sessions:
            if path and s.get("sessionId") and s["sessionId"] in path:
                cur = s
                break
        if cur is None and sessions:
            cur = sessions[0]

        return blank(state,
                     task=self._scan_last_prompt(path),
                     cwd=(cur or {}).get("cwd"),
                     sessionId=(cur or {}).get("sessionId"),
                     lastActivity=(rec or {}).get("timestamp"),
                     activeSessions=len(sessions),
                     source=self.name)


# ---------------------------------------------------------------- Codex

class CodexSource(BaseSource):
    """读 Codex CLI 的本地会话记录。

    格式要点（实测）：
      · 路径 ~/.codex/sessions/<年>/<月>/<日>/rollout-*.jsonl
      · timestamp 是 **UTC**，形如 2026-09-23T02:18:30.269Z
      · 状态信号在 response_item.payload.type 上：
          reasoning / custom_tool_call / function_call / message
        event_msg.payload.type 另有 task_started / task_complete / turn_aborted
      · 没有心跳文件，所以用「最后一条事件距今多久」判断是否活跃
    """
    name = "codex"
    title = "Codex"

    ACTIVE_MS = 120000      # 2 分钟内有事件 = 还活着

    def __init__(self, root=None):
        # root 可覆盖，便于用临时目录做测试（不然只有真实数据能测）
        self.root = root or CODEX_SESSIONS

    def available(self):
        return os.path.isdir(self.root)

    def _latest(self):
        cands = glob.glob(os.path.join(self.root, "*", "*", "*", "rollout-*.jsonl"))
        if not cands:
            return None
        return max(cands, key=os.path.getmtime)

    @staticmethod
    def _ts_ms(s):
        if not s:
            return 0
        try:
            t = s.rstrip("Z")
            if "." in t:
                base, frac = t.split(".")
                frac = (frac + "000000")[:6]
                t = base + "." + frac
            d = dt.datetime.strptime(t, "%Y-%m-%dT%H:%M:%S.%f").replace(tzinfo=dt.timezone.utc)
            return int(d.timestamp() * 1000)
        except Exception:
            return 0

    def _tail(self, path, limit=400, bytes_=600_000):
        try:
            size = os.path.getsize(path)
            with open(path, "rb") as fh:
                if size > bytes_:
                    fh.seek(-bytes_, os.SEEK_END)
                    fh.readline()
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

    def _last_user_text(self, recs):
        for r in reversed(recs):
            pl = r.get("payload") or {}
            if r.get("type") == "response_item" and pl.get("type") == "message":
                if pl.get("role") == "user":
                    c = pl.get("content")
                    txt = None
                    if isinstance(c, list):
                        for part in c:
                            if isinstance(part, dict) and part.get("text"):
                                txt = part["text"]
                                break
                    elif isinstance(c, str):
                        txt = c
                    got = _clean_text(txt)
                    if got and not _looks_like_noise(got):
                        return got[:140]
        return None

    def snapshot(self):
        path = self._latest()
        if not path:
            return blank("idle", source=self.name)
        recs = self._tail(path)
        if not recs:
            return blank("idle", source=self.name)

        last_ts = self._ts_ms(recs[-1].get("timestamp"))
        age = now_ms() - last_ts if last_ts else 10 ** 9
        if age > self.ACTIVE_MS:
            return blank("idle", lastActivity=last_ts, source=self.name)

        state, rec = "idle", None
        for r in reversed(recs):
            ts = self._ts_ms(r.get("timestamp"))
            if ts and now_ms() - ts > 180000:
                break
            pl = r.get("payload") or {}
            t = r.get("type")
            pt = pl.get("type")

            if t == "event_msg":
                if pt == "task_complete":
                    state, rec = "success", r
                    break
                if pt == "turn_aborted":
                    state, rec = "error", r
                    break
                if pt == "task_started":
                    state, rec = "thinking", r
                    break
                continue

            if t != "response_item":
                continue
            if pt in ("custom_tool_call", "function_call"):
                state, rec = "working", r
                break
            if pt in ("custom_tool_call_output", "function_call_output"):
                state, rec = ("working" if (ts and now_ms() - ts < FRESH_MS) else "thinking"), r
                break
            if pt == "reasoning":
                if ts and now_ms() - ts < FRESH_MS:
                    state, rec = "thinking", r
                    break
                continue
            if pt == "message":
                age = (now_ms() - ts) if ts else 10 ** 9
                if pl.get("role") == "user":
                    # 球在 Agent 这边，不该是 waiting
                    state = "thinking" if age < WAITING_MS else "idle"
                else:
                    if age < SUCCESS_WINDOW_MS:
                        state = "success"
                    elif age < WAITING_MS:
                        state = "waiting"
                    else:
                        state = "idle"
                rec = r
                break

        cwd = None
        for r in recs:
            if r.get("type") == "session_meta":
                cwd = ((r.get("payload") or {}).get("cwd")) or cwd
                break

        return blank(state,
                     task=self._last_user_text(recs),
                     cwd=cwd,
                     sessionId=os.path.basename(path)[:36],
                     lastActivity=(self._ts_ms((rec or {}).get("timestamp")) or last_ts),
                     activeSessions=1,
                     source=self.name)


# ---------------------------------------------------------------- 工具函数

NOISE = ("background command", "system-reminder", "completed", "call_")


def _looks_like_noise(txt):
    low = txt.lower()
    if "background command" in low or "system-reminder" in low:
        return True
    if "call_" in low and ("completed" in low or "started" in low or "failed" in low):
        return True
    if low.startswith("blocode") or "&quot;" in low:
        return True
    return len(txt) < 5


def _clean_text(txt):
    if not txt:
        return None
    txt = re.sub(r"<system-reminder.*?</system-reminder>", " ", txt, flags=re.S)
    txt = re.sub(r'@long-text:"[^"]*"', " ", txt)
    txt = re.sub(r"@image#\d+:\S+", " ", txt)
    txt = re.sub(r"@\S+\.(png|jpg|jpeg|gif|webp|pdf|docx|xlsx|txt|md)", " ", txt, flags=re.I)
    txt = re.sub(r"<[^>]{1,80}>", " ", txt)
    txt = re.sub(r"\s+", " ", txt).strip()
    return txt or None


# ---------------------------------------------------------------- 选择

REGISTRY = {
    "workbuddy": WorkBuddySource,
    "codex": CodexSource,
    "none": NullSource,
}


def pick(name="auto"):
    """返回一个状态源实例。

    auto：优先挑「此刻真的有活动」的那个；都没有就用 none（宠物自己做 idle）。
    """
    name = (name or "auto").lower()
    if name in REGISTRY:
        return REGISTRY[name]()
    # auto
    best, best_score = None, -1
    for key in ("workbuddy", "codex"):
        try:
            s = REGISTRY[key]()
            if not s.available():
                continue
            snap = s.snapshot()
            st = snap.get("state")
            score = 2 if st in ("working", "thinking", "success", "error", "waiting") else \
                    1 if st == "idle" else 0
            if score > best_score:
                best, best_score = s, score
        except Exception:
            continue
    return best or NullSource()


def describe():
    """列出本机有哪些状态源可用（给 CLI --list 用）"""
    rows = []
    for key, cls in REGISTRY.items():
        s = cls()
        rows.append((key, cls.title, s.available()))
    return rows
