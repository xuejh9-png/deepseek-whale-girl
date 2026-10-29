#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
宠物状态源适配器 —— 「Agent 现在在干什么」从哪读。

运行时（网页 + 窗口）只认一个契约：GET /state 返回
    {"agent": {"state": ..., "label": ..., "task": ..., "cwd": ..., ...}}

状态名固定这几个（运行时的状态机按它们选剪辑）：
    idle / thinking / working / success / waiting / error / sleeping / offline

其中 `sleeping` 不是从记录里直接读出来的，而是**推出来的**：
「球不在 Agent 手里」（success / waiting / idle）且已经安静 `SLEEP_MS`（1 分钟）
→ sleeping。working / thinking 是正在干活，再久也不睡。
每条时间线见 `_sleep_if_quiet`。

⚠️ 不是每个源都能产出全部状态（实测审计结果）：
    WorkBuddy → 无 error（日志里没有错误类型记录）
    Codex     → 无 waiting
新增适配器时请一并确认「声明的状态是否真的会被产出」——
`run` 和 `sleeping` 都曾经"写了但永远走不到"，做了等于没做。

每个适配器只负责把「某个工具的本地记录」翻译成这个契约。
新增一个工具 = 新增一个类，运行时一行都不用改。

现有：
    WorkBuddySource  读 ~/.workbuddy/projects/*.jsonl + sessions/*.json
    CodexSource      读 ~/.codex/sessions/**/rollout-*.jsonl
    ClaudeSource     读 ~/.claude/projects/*/*.jsonl
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
WAITING_MS = 45000         # 静默多久算「空闲」
SLEEP_MS = 60000           # 静默多久算「睡着」（1 分钟）

# 哪些状态在"安静够久"之后可以睡着。
# ⚠️ **故意不含 working / thinking** —— 那两个是"正在干活"，
#    就算记录很久没更新也不该睡（那是卡住了，不是没事干）。
#    offline 也不含：它没有可信的活动时间戳，不猜。
SLEEPY_STATES = ("success", "waiting", "idle")


def _sleep_if_quiet(state, last_activity, now):
    """「球不在 Agent 手里」且已安静够久 → 睡着。

    这条补的是**一个从没被触发过的状态**：`sleeping` 一直写在状态表、
    标签表和运行时的剪辑映射里，但**没有任何一个适配器会产出它** ——
    所以 sleep 素材交付了却从没播过（和 run 是同一类问题：做完了没接上）。

    参考时刻用 lastActivity（最后一次记录的毫秒时间戳），取不到就不睡 —— 不猜。
    """
    if state not in SLEEPY_STATES or not last_activity:
        return state
    return "sleeping" if (now - last_activity) >= SLEEP_MS else state


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
    def _active_sessions(self, now, paths):
        """「这个会话还活着」有两个证据，满足任一条即可：

        ① 心跳新鲜 —— 进程还开着（WorkBuddy 客户端写的）
        ② 会话日志刚被写过 —— 真的在干活

        ⚠️ ② 不能省。实测本机 `sessions/*.json` 的心跳**只在会话启动时写一次**
        （startedAt / lastHeartbeat / updatedAt 是同一秒），一分钟后就"过期"了，
        而 `projects/*/*.jsonl` 一直在写。只认心跳会得到两个后果：
        正在干活的会话被踢出列表，宠物转而去读另一个心跳还在刷、
        但七天没活动的僵尸会话 —— 用户明明在跑任务，卡片上却是「睡着啦」。
        """
        out = []
        for p in glob.glob(os.path.join(SESSIONS_DIR, "*.json")):
            try:
                with open(p, "r", encoding="utf-8") as fh:
                    d = json.load(fh)
            except Exception:
                continue
            sid = d.get("sessionId")
            if not isinstance(sid, str) or not sid:
                continue
            hb = d.get("lastHeartbeat") or 0
            hb_fresh = isinstance(hb, (int, float)) and 0 <= now - hb < SLEEP_MS
            log_ms = self._log_write_ms(paths, sid)
            log_fresh = log_ms > 0 and 0 <= now - log_ms < SLEEP_MS
            if hb_fresh or log_fresh:
                d["_logWrite"] = log_ms
                d["_hbFresh"] = hb_fresh
                out.append(d)
        return out

    @staticmethod
    def _log_write_ms(paths, sid):
        """该会话日志最后一次写入的毫秒时间戳；文件不存在/有歧义则 0。"""
        matches = paths.get(sid, [])
        if len(matches) != 1:
            return 0
        try:
            return int(os.path.getmtime(matches[0]) * 1000)
        except OSError:
            return 0

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

    def tasks_snapshot(self):
        now = now_ms()
        paths = {}
        for path in glob.glob(os.path.join(PROJECTS_DIR, "*", "*.jsonl")):
            paths.setdefault(os.path.basename(path)[:-6], []).append(path)
        active = {}
        for session in self._active_sessions(now, paths):
            sid = session["sessionId"]
            if not isinstance(sid, str):
                continue
            if sid not in active or session["lastHeartbeat"] > active[sid]["lastHeartbeat"]:
                active[sid] = session
        tasks = []
        for sid, session in sorted(active.items()):
            matches = paths.get(sid, [])
            # Never pair a fresh heartbeat with another task's latest file.
            # Missing/renamed/ambiguous files disappear until identity is valid.
            if len(matches) != 1:
                continue
            recs = self._read_tail(matches[0])
            if not recs:
                continue
            task = self._session_snapshot(matches[0], recs, session, now)
            task.update(id="workbuddy:" + sid,
                        heartbeatFresh=bool(session.get("_hbFresh")),
                        lastHeartbeat=session["lastHeartbeat"],
                        lastWrite=session.get("_logWrite") or 0)
            tasks.append(task)
        for task in tasks:
            task["activeSessions"] = len(tasks)
        return tasks

    def snapshot(self):
        tasks = self.tasks_snapshot()
        self.tasks = tasks
        if not tasks:
            return blank("offline", source=self.name)
        # Completion of one session must not mask another executing session.
        # 同状态时选**最后真的动过**的那个：僵尸会话的心跳可能比谁都新，
        # 但它的日志几天没写了，不该抢过刚被写过的会话。
        priority = {"working": 0, "thinking": 1, "error": 2, "waiting": 3,
                    "success": 4, "idle": 5, "sleeping": 6}
        return min(tasks, key=lambda t: (priority.get(t["state"], 9),
                                         -(t.get("lastActivity") or 0),
                                         t["id"]))

    def _session_snapshot(self, path, recs, session, now):
        state, rec = "idle", {}
        newest_ts = 0
        for r in reversed(recs):
            ts = r.get("timestamp") or 0
            if not ts:
                continue
            if not newest_ts:
                newest_ts = ts          # 循环会因为 180s 窗口提前 break，
                                        # 但"最后一次活动是什么时候"要留下 ——
                                        # 否则安静超过 3 分钟时反而取不到参考时刻、永远不睡
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

        la = (rec or {}).get("timestamp") or newest_ts
        state = _sleep_if_quiet(state, la, now)
        return blank(state,
                     task=self._scan_last_prompt(path),
                     cwd=session.get("cwd"),
                     sessionId=session.get("sessionId"),
                     lastActivity=la,
                     lastHeartbeat=session.get("lastHeartbeat"),
                     activeSessions=1,
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
            # 这条早退也要过睡眠规则 —— 否则"早就停了"永远停在 idle，
            # 新加的 sleeping 就只能靠窄路径触发（写完发现走不到，等于白写）
            return blank(_sleep_if_quiet("idle", last_ts, now_ms()),
                         lastActivity=last_ts, source=self.name)

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

        la = self._ts_ms((rec or {}).get("timestamp")) or last_ts
        state = _sleep_if_quiet(state, la, now_ms())
        return blank(state,
                     task=self._last_user_text(recs),
                     cwd=cwd,
                     sessionId=os.path.basename(path)[:36],
                     lastActivity=la,
                     activeSessions=1,
                     source=self.name)


# ---------------------------------------------------------------- 自动跟随

class AutoSource(BaseSource):
    """每次请求都重新比较各工具，挑「此刻真的在动」的那个。

    为什么不能只在启动时选一次：原来的 auto 在进程启动那一刻按快照打分，
    选中 Codex 之后就一直读 Codex。用户明明在用 WorkBuddy，宠物却永远显示
    Codex 那边的「睡着啦」——卡片就变成了死的（用户实测截图确认）。
    所以选择必须放进每次 snapshot()，而不是进程生命周期。
    """

    name = "auto"
    title = "自动跟随当前活动的工具"

    ACTIVE = ("working", "thinking", "error", "waiting")
    RANK = {"working": 0, "thinking": 1, "error": 2, "waiting": 3,
            "success": 4, "idle": 5, "sleeping": 6, "offline": 7}

    def __init__(self):
        self._sources = [WorkBuddySource(), CodexSource(), ClaudeSource()]
        self.tasks = []
        self.sources = []

    def available(self):
        return any(s.available() for s in self._sources)

    def snapshot(self):
        cands = []
        for src in self._sources:
            try:
                if not src.available():
                    continue
                snap = src.snapshot()
            except Exception:
                continue
            if src.name == "workbuddy":
                # 任务列表永远来自 WorkBuddy；动画状态才由下面的比较决定。
                self.tasks = getattr(src, "tasks", [])
            cands.append((src, snap))
        self.sources = [{"name": s.name, "state": d.get("state"),
                         "lastActivity": d.get("lastActivity")} for s, d in cands]
        if not cands:
            self.tasks = []
            return blank("offline", source=self.name)
        return min(cands, key=self._key)[1]

    @classmethod
    def _key(cls, pair):
        src, snap = pair
        state = snap.get("state")
        active = 1 if state in cls.ACTIVE else 0
        # 只认「最后一次真的有动静」的日志时间。
        # ⚠️ 不要把 lastHeartbeat 算进来：心跳只证明进程开着，不证明有活干。
        #    实测一个七天没活动的僵尸会话，心跳一直在刷 —— 它的心跳会永远
        #    压过用户此刻正在跑的那个会话（心跳只在启动时写过一次）。
        fresh = snap.get("lastActivity") or 0
        # 先看谁在动，再看谁最后动过，最后才按状态语义；同分时 WorkBuddy 优先。
        return (-active, -fresh, cls.RANK.get(state, 9),
                0 if src.name == "workbuddy" else 1)


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


# ---------------------------------------------------------------- Claude Code

class ClaudeSource(CodexSource):
    """与 Windows 共用同一组业务事件验收样例；不把元数据当工作信号。"""
    name = "claude"
    title = "Claude Code"
    ACTIVE_STALE_MS = 30 * 60 * 1000

    def __init__(self, root=None):
        self.root = root or os.path.expanduser(os.environ.get(
            "PET_CLAUDE_DIR", os.path.join(os.environ.get("CLAUDE_CONFIG_DIR", "~/.claude"), "projects")))
        self._cache = {}

    @staticmethod
    def _ts_ms(value):
        try:
            stamp = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
            if stamp.tzinfo is None:
                stamp = stamp.replace(tzinfo=dt.timezone.utc)
            return int(stamp.timestamp() * 1000)
        except (AttributeError, ValueError, TypeError):
            return 0

    @staticmethod
    def _event_state(rec):
        if rec.get("isSidechain"):
            return None
        msg = rec.get("message") or {}
        content = msg.get("content")
        parts = content if isinstance(content, list) else []
        types = {p.get("type") for p in parts if isinstance(p, dict)}
        kind = rec.get("type")
        if kind == "assistant":
            stop = msg.get("stop_reason")
            if "tool_use" in types or stop == "tool_use":
                return "working"
            if stop in ("end_turn", "stop_sequence"):
                return "success"
            if "thinking" in types and "text" not in types:
                return "thinking"
            if "text" in types:
                return "thinking" if "stop_reason" in msg and stop is None else "success"
        if kind == "user":
            if "tool_result" in types:
                return "thinking"
            if isinstance(content, str) and content.startswith("[Request interrupted by user"):
                return "error"
            if isinstance(content, str) or "text" in types:
                return "thinking"
        if kind == "api-request":
            return "thinking"
        if kind == "progress":
            return "working"
        if kind == "system" and rec.get("subtype") == "api_error":
            return "error"
        return None

    def snapshot(self):
        paths = glob.glob(os.path.join(self.root, "*", "*.jsonl"))
        self._cache = {p: v for p, v in self._cache.items() if p in paths}
        best = None
        for path in paths:
            try:
                stat = os.stat(path)
            except OSError:
                continue
            key = (stat.st_mtime_ns, stat.st_size)
            cached = self._cache.get(path)
            if cached is None or cached[0] != key:
                recs = self._tail(path, limit=200, bytes_=120000)
                cwd = session_id = None
                for rec in recs:
                    cwd = rec.get("cwd") or cwd
                    session_id = rec.get("sessionId") or session_id
                info = None
                for rec in reversed(recs):
                    ts = self._ts_ms(rec.get("timestamp"))
                    state = self._event_state(rec)
                    if ts and state:
                        info = (ts, state, cwd, session_id)
                        break
                self._cache[path] = (key, info)
            info = self._cache[path][1]
            if info and (best is None or info[0] > best[0]):
                best = info
        if best is None:
            return blank("offline", source=self.name)
        ts, state, cwd, session_id = best
        age = now_ms() - ts
        if state in ("working", "thinking"):
            if age >= self.ACTIVE_STALE_MS:
                state = "sleeping"
        elif age >= SLEEP_MS:
            state = "sleeping"
        elif state == "success" and age >= WAITING_MS:
            state = "idle"
        elif state == "success" and age >= SUCCESS_WINDOW_MS:
            state = "waiting"
        return blank(state, source=self.name, cwd=cwd, sessionId=session_id,
                     lastActivity=ts, activeSessions=1)


# ---------------------------------------------------------------- 选择

REGISTRY = {
    "workbuddy": WorkBuddySource,
    "codex": CodexSource,
    "claude": ClaudeSource,
    "none": NullSource,
}


def pick(name="auto"):
    """返回一个状态源实例。

    auto：返回 AutoSource —— **每次请求**都比较各工具，跟随此刻活动的那个。
    其余名字返回固定源；都没有就用 none（宠物自己做 idle）。
    """
    name = (name or "auto").lower()
    if name in REGISTRY:
        return REGISTRY[name]()
    return AutoSource()


def describe():
    """列出本机有哪些状态源可用（给 CLI --list 用）"""
    rows = []
    for key, cls in REGISTRY.items():
        s = cls()
        rows.append((key, cls.title, s.available()))
    return rows
