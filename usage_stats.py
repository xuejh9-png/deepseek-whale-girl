#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
本机 AI 工具 Token 用量统计（纯 Python 标准库，零依赖）

两个来源，合并统计：
  ChatGPT（桌面版，程序内部叫 Codex）~/.codex/sessions/<年>/<月>/<日>/rollout-*.jsonl
         token_usage_record 事件里的 payload.usage（input / output / total）
  DeepSeek（WorkBuddy） ~/.workbuddy/projects/<项目>/<会话ID>.jsonl
         每条 LLM 调用都带 providerData.rawUsage（prompt / completion / total）
         这是逐次调用的完整记账，也是本地唯一可信的 token 来源
         （traces/ 目录里的 totalTokens 覆盖率仅 13%，不能用）

口径（两个来源一致，可直接相加对比）：
  累计 Token  = sum(total_tokens)，即 输入 + 输出；
                缓存重读（cached）计入输入，不单独剔除 —— 与接口账单口径一致。

⚠️ 两处去重：
  ChatGPT 一个会话被「续写」会生成第二个 rollout 文件并**整段重放历史**，
         必须按 response_id 去重，否则同一个响应会被计两次。
  WorkBuddy 按 (时间戳, 输入, 输出, 模型) 四元组防重放。

用法：
    python3 usage_stats.py                # 全部历史（默认，含 ChatGPT + DeepSeek）
    python3 usage_stats.py --month 2026-10
    python3 usage_stats.py --days 7       # 最近 7 天
    python3 usage_stats.py --source codex # 只看某一个来源
    python3 usage_stats.py --no-html      # 只打印，不生成 HTML
    python3 usage_stats.py --open         # 生成后自动打开报告
"""

import argparse
import collections
import datetime as dt
import glob
import html
import json
import os
import re
import sqlite3
import sys
import threading

WB_ROOT = os.path.expanduser("~/.workbuddy")
PROJECTS_DIR = os.path.join(WB_ROOT, "projects")
DB_PATH = os.path.join(WB_ROOT, "workbuddy.db")

CODEX_ROOT = os.path.expanduser("~/.codex")
CODEX_SESSIONS = os.path.join(CODEX_ROOT, "sessions")
CODEX_INDEX = os.path.join(CODEX_ROOT, "session_index.jsonl")

OUT_HTML = os.path.join(os.path.dirname(os.path.abspath(__file__)), "用量报告.html")

# 来源（= 本机的 AI 工具）。柱状图按这个维度分色。
# 顺序 = 图例从左到右 = 柱子里分段自上而下（柱是 column flex，第一个子元素在最上面）。
SRC_CODEX = "ChatGPT"
SRC_DEEPSEEK = "DeepSeek"
# Claude Code 的会话日志目录。默认 ~/.claude/projects；
# 装在别处/容器里/换过 CLAUDE_CONFIG_DIR 的机器可以用 PET_CLAUDE_DIR 指过来。
CLAUDE_PROJECTS = os.path.expanduser(os.environ.get("PET_CLAUDE_DIR") or "~/.claude/projects")
SRC_CLAUDE = "Claude Code"
SRC_OTHER = "其他模型"
SOURCE_ORDER = (SRC_DEEPSEEK, SRC_CODEX, SRC_CLAUDE, SRC_OTHER)   # 图例顺序 = 柱内分段自上而下
SOURCE_COLOR = {SRC_DEEPSEEK: "#2b6cff", SRC_CODEX: "#7c3aed", SRC_CLAUDE: "#bc6b45", SRC_OTHER: "#94a3b8"}

# ---------------------------------------------------------------- 法定工作日
#
# 「只看工作日」要跳过的不只是周末，还有法定假日和**调休**：调休上班的周六/周日
# 是真上班，得算工作日；放假的工作日不算。所以周末规则不够用，必须查表。
#
# 来源：国务院办公厅《关于2026年部分节假日安排的通知》（2025-11-04 发布，
#       gov.cn）。表里的日期已逐条与该通知中标注的星期核对过。
#
# ⚠️ 每年 11 月左右会发下一年度的通知，**到期必须手工补**，否则调休会算错。
#    2027 年的通知出来之前，2027 年的日期会退化成"周一~周五算工作日"。
HOLIDAYS_2026 = (
    "2026-01-01", "2026-01-02", "2026-01-03",                        # 元旦
    "2026-02-15", "2026-02-16", "2026-02-17", "2026-02-18", "2026-02-19",
    "2026-02-20", "2026-02-21", "2026-02-22", "2026-02-23",          # 春节
    "2026-04-04", "2026-04-05", "2026-04-06",                        # 清明
    "2026-05-01", "2026-05-02", "2026-05-03", "2026-05-04", "2026-05-05",  # 劳动节
    "2026-06-19", "2026-06-20", "2026-06-21",                        # 端午
    "2026-09-25", "2026-09-26", "2026-09-27",                        # 中秋
    "2026-10-01", "2026-10-02", "2026-10-03", "2026-10-04", "2026-10-05",
    "2026-10-06", "2026-10-07",                                      # 国庆
)
# 调休上班的周末（通知里明写"上班"的那几天）
MAKEUP_WORKDAYS = (
    "2026-01-04",                       # 周日上班
    "2026-02-14", "2026-02-28",         # 周六上班
    "2026-05-09",                       # 周六上班
    "2026-09-20",                       # 周日上班
    "2026-10-10",                       # 周六上班
)


def is_workday(d):
    """d 是 datetime.date。调休上班的周末算工作日，法定假日不算。"""
    s = d.strftime("%Y-%m-%d")
    if s in MAKEUP_WORKDAYS:
        return True
    if s in HOLIDAYS_2026:
        return False
    return d.weekday() < 5



# ---------------------------------------------------------------- 数据采集

MODEL_ALIAS = (
    ("v4.1", "flash", "DeepSeek-V4.1-Flash"),
    ("v4.1", "pro", "DeepSeek-V4.1-Pro"),
    ("v4", "pro", "DeepSeek-V4-Pro"),
    ("v4", "flash", "DeepSeek-V4-Flash"),
    ("r1", "", "DeepSeek-R1"),
)


def norm_model(name):
    """把 deepseek-v4.1-flash / Deepseek-V4.1-Flash 之类合并成统一展示名。"""
    s = (name or "").lower()
    if s.startswith("gpt"):
        return norm_gpt(name)
    if "deepseek" not in s:
        return name or "未知模型"
    for a, b, label in MODEL_ALIAS:
        if a in s and (not b or b in s):
            return label
    return name


def norm_gpt(name):
    """gpt-5.6-luna → GPT-5.6-Luna（ChatGPT 侧的模型名）。"""
    return "-".join(p.upper() if p == "gpt" else p.capitalize()
                    for p in (name or "").split("-"))


def is_deepseek(name):
    return "deepseek" in (name or "").lower()


def source_of(model, origin):
    """把一次调用归到某个来源 —— 柱状图的分色维度。

    origin 是数据来自哪个工具（codex / workbuddy）。WorkBuddy 上跑的
    DeepSeek 系模型归入 DeepSeek；将来换成别的模型会自动落到「其他模型」，
    不用改代码。
    """
    if origin == "codex":
        return SRC_CODEX
    return SRC_DEEPSEEK if is_deepseek(model) else SRC_OTHER


def _iso_ms(s):
    """ISO-8601 (including explicit offsets) to epoch milliseconds."""
    try:
        value = dt.datetime.fromisoformat(str(s).replace("Z", "+00:00"))
        if value.tzinfo is None:
            return 0  # ambiguous dates must not silently become UTC
        return int(value.timestamp() * 1000)
    except (ValueError, TypeError, OverflowError):
        return 0


def is_claude(model):
    return isinstance(model, str) and bool(re.search(
        r"(?:^|[/.:])claude-(?:opus|sonnet|haiku|[0-9])", model.lower()))


def _tokens(usage, keys):
    values = [usage.get(k, 0) for k in keys]
    if any(type(v) is not int or v < 0 for v in values):
        return None
    return values


# ---------------------------------------------------------------- 增量解析缓存
#
# 宠物的卡片每隔几秒就要算一次「今日 Token」，而下面三个 reader 每次都要把
# 全量日志重新逐行 json.loads 一遍（本机实测 WorkBuddy ≈2.9s、Codex ≈0.9s）。
# 服务端因此只能把结果缓存 20 秒 —— 用户看到的数字就不是实时的。
#
# 这里按「文件 mtime + size」缓存每个文件解析出来的记录：日志只会追加，
# 任何追加都会改变 size，命中缓存时直接复用上次的结果。语义不变
#（返回的还是同一批 dict），调用方一行都不用改；只有变动的文件需要重解析。

_PARSE_CACHE = {}
_PARSE_LOCK = threading.Lock()
_PARSE_MAX_FILES = 4096


def _file_sig(path):
    try:
        st = os.stat(path)
    except OSError:
        return None
    return (st.st_mtime_ns, st.st_size)


def _cached_parse(path, parser):
    sig = _file_sig(path)
    if sig is None:
        return []
    # 同一个日志可能被两个 reader 用不同口径解析（Codex 的逐请求账本与累计账本），
    # 所以缓存键必须带解析器名字，不能只用路径。
    key = (parser.__name__, path)
    with _PARSE_LOCK:
        hit = _PARSE_CACHE.get(key)
        if hit is not None and hit[0] == sig:
            return hit[1]
    records = parser(path)
    with _PARSE_LOCK:
        if len(_PARSE_CACHE) >= _PARSE_MAX_FILES:
            _PARSE_CACHE.clear()
        _PARSE_CACHE[key] = (sig, records)
    return records


def load_session_meta():
    """从本地库读会话标题和项目路径，读不到就降级为空。"""
    meta = {}
    if not os.path.exists(DB_PATH):
        return meta
    try:
        con = sqlite3.connect("file:%s?mode=ro" % DB_PATH, uri=True)
        rows = con.execute("SELECT id, title, custom_title, cwd FROM sessions").fetchall()
        for sid, title, custom, cwd in rows:
            meta[sid] = {"title": (custom or title or "").strip(), "cwd": cwd or ""}
        con.close()
    except Exception:
        pass
    return meta


def _parse_workbuddy_file(path):
    """单个 WorkBuddy 会话日志 → 调用记录列表（不含跨文件去重）。"""
    project = os.path.basename(os.path.dirname(path))
    session = os.path.basename(path)[:-6]
    out = []
    try:
        fh = open(path, "r", encoding="utf-8", errors="ignore")
    except OSError:
        return out
    with fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            pd = rec.get("providerData") or {}
            ru = pd.get("rawUsage")
            if not ru:
                continue
            ts = rec.get("timestamp") or 0
            prompt = ru.get("prompt_tokens", 0) or 0
            completion = ru.get("completion_tokens", 0) or 0
            total = ru.get("total_tokens", 0) or (prompt + completion)
            model = norm_model(pd.get("requestModelName") or pd.get("model") or "未知模型")
            details = ru.get("prompt_tokens_details") or {}
            out.append({
                "ts": ts,
                "when": dt.datetime.fromtimestamp(ts / 1000.0),
                "prompt": prompt,
                "completion": completion,
                "total": total,
                "cached": details.get("cached_tokens", 0) or 0,
                "model": model,
                "source": source_of(model, "workbuddy"),
                "project": project,
                "session": session,
            })
    return out


def iter_calls():
    """逐条产出每次 LLM 调用的用量记录。"""
    seen = set()
    pattern = os.path.join(PROJECTS_DIR, "*", "*.jsonl")
    for path in sorted(glob.glob(pattern)):
        for rec in _cached_parse(path, _parse_workbuddy_file):
            # 防重放：同毫秒 + 同用量 + 同模型 视为同一次（跨文件，不能进缓存）
            key = (rec["ts"], rec["prompt"], rec["completion"], rec["model"])
            if key in seen:
                continue
            seen.add(key)
            yield rec


def load_codex_titles():
    """ChatGPT 会话标题表（thread_id → 名称）。读不到就降级为空。"""
    titles = {}
    if not os.path.exists(CODEX_INDEX):
        return titles
    try:
        with open(CODEX_INDEX, "r", encoding="utf-8", errors="ignore") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if rec.get("id"):
                    titles[rec["id"]] = (rec.get("thread_name") or "").strip()
    except OSError:
        pass
    return titles


def _parse_codex_response_file(path):
    """单个 rollout 文件 → (turn_id→model, token_usage_record 列表, cwd)。"""
    turn_model, pending, cwd = {}, [], ""
    try:
        fh = open(path, "r", encoding="utf-8", errors="ignore")
    except OSError:
        return turn_model, pending, cwd
    with fh:
        for line in fh:
            if "token_usage_record" not in line and "turn_context" not in line:
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            rtype = rec.get("type")
            payload = rec.get("payload") or {}
            if rtype == "turn_context":
                tid = payload.get("turn_id")
                if tid:
                    turn_model[tid] = payload.get("model") or ""
                if not cwd:
                    cwd = payload.get("cwd") or ""
            elif rtype == "token_usage_record":
                pending.append(rec)
    return turn_model, pending, cwd


def _iter_codex_responses():
    """逐条产出 ChatGPT 桌面版（程序内部叫 Codex）每次调用的用量记录。

    数据在 sessions/<年>/<月>/<日>/rollout-*.jsonl 里，关心两种事件：
        turn_context        → 本次 turn 用的模型（token_usage_record 自己不记模型）
        token_usage_record  → payload.usage，逐次调用的增量用量

    ⚠️ 两个坑：
    1. 一个会话被「续写」会新开一个 rollout 文件并**整段重放历史**，
       必须按 response_id 去重，否则同一个响应被计两次。
    2. 目录整体接近 900MB，逐行 json.loads 会很慢 ——
       先用子串过滤掉不关心的大行（base_instructions / 消息体），只解析必要的行。
       按文件缓存解析结果后，重复统计只重读变动过的文件。
    """
    if not os.path.isdir(CODEX_SESSIONS):
        return
    titles = load_codex_titles()
    pattern = os.path.join(CODEX_SESSIONS, "*", "*", "*", "*.jsonl")
    seen = set()
    for path in sorted(glob.glob(pattern)):
        turn_model, pending, cwd = _cached_parse(path, _parse_codex_response_file)
        for rec in pending:
            payload = rec.get("payload") or {}
            usage = payload.get("usage") or {}
            rid = payload.get("response_id")
            if rid:
                if rid in seen:
                    continue
                seen.add(rid)
            if not rid:
                fallback = (payload.get("thread_id") or payload.get("session_id"),
                            rec.get("timestamp"), payload.get("turn_id"), json.dumps(usage, sort_keys=True))
                if fallback in seen:
                    continue
                seen.add(fallback)
            ts = _iso_ms(rec.get("timestamp"))
            if not ts:
                continue
            prompt = usage.get("input_tokens", 0) or 0
            completion = usage.get("output_tokens", 0) or 0
            total = usage.get("total_tokens", 0) or (prompt + completion)
            raw_model = (turn_model.get(payload.get("turn_id")) or "").strip()
            model = norm_model(raw_model) if raw_model else "GPT（未标注）"
            thread = payload.get("thread_id") or payload.get("session_id") or ""
            yield {
                "ts": ts,
                "when": dt.datetime.fromtimestamp(ts / 1000.0),
                "prompt": prompt,
                "completion": completion,
                "total": total,
                "cached": usage.get("cached_input_tokens", 0) or 0,
                "model": model,
                "source": SRC_CODEX,
                "turn": payload.get("turn_id") or "",
                "title": titles.get(thread, ""),
                "project": cwd.replace(os.path.expanduser("~"), "~") if cwd else "ChatGPT",
                # 前缀避免和 WorkBuddy 的会话 id 撞车（两边都是 uuid）
                "session": "codex:" + thread if thread else path,
            }


def _parse_codex_snapshot_file(path):
    """单个 rollout 文件 → (session id, [(ts, turn, model, cwd, totals)])。"""
    sid, turn, model, cwd = path, "", "", ""
    events = []
    try:
        fh = open(path, encoding="utf-8", errors="ignore")
    except OSError:
        return sid, events
    with fh:
        for line in fh:
            if not any(k in line for k in ("session_meta", "turn_context", "token_count")):
                continue
            try:
                r = json.loads(line)
            except ValueError:
                continue
            pl = r.get("payload") or {}
            if r.get("type") == "session_meta":
                sid = "codex:" + str(pl.get("id") or path)
                cwd = pl.get("cwd") or cwd
            elif r.get("type") == "turn_context":
                turn = pl.get("turn_id") or turn
                model = pl.get("model") or model
                cwd = pl.get("cwd") or cwd
            elif r.get("type") == "event_msg" and pl.get("type") == "token_count":
                usage = (pl.get("info") or {}).get("total_token_usage")
                ts = _iso_ms(r.get("timestamp"))
                if not usage or not ts:
                    continue
                values = _tokens(usage, ("input_tokens", "output_tokens", "cached_input_tokens"))
                if values is not None:
                    events.append((ts, turn, model, cwd, values))
    return sid, events


def iter_codex_calls():
    """Prefer request-ID usage; cumulative snapshots only fill uncovered turns.

    These are distinct ledgers: thread_token_usage and token_count totals can
    have different offsets. Never interleave them in a single delta baseline.
    """
    responses = list(_iter_codex_responses())
    covered = {(r["session"], r["turn"]) for r in responses}
    yield from responses
    snapshots = collections.defaultdict(list)
    for path in sorted(glob.glob(os.path.join(CODEX_SESSIONS, "*", "*", "*", "*.jsonl"))):
        sid, events = _cached_parse(path, _parse_codex_snapshot_file)
        snapshots[sid].extend(events)
    for sid, events in snapshots.items():
        previous, seen = [0, 0, 0], set()
        for ts, turn, model, cwd, current in sorted(events, key=lambda row: row[0]):
            key = (turn, tuple(current))
            if key in seen:
                continue
            seen.add(key)
            reset = current[0] < previous[0] or current[1] < previous[1]
            delta = current if reset else [max(0, v - old) for v, old in zip(current, previous)]
            previous = current
            # Always advance the snapshot baseline, including covered turns.
            if (sid, turn) in covered or (sid, "") in covered:
                continue
            prompt, completion, cached = delta
            if not prompt + completion:
                continue
            yield {"ts": ts, "when": dt.datetime.fromtimestamp(ts / 1000),
                   "prompt": prompt, "completion": completion, "cached": cached,
                   "total": prompt + completion, "model": norm_model(model) if model else "GPT（未标注）",
                   "source": SRC_CODEX, "title": "", "project": cwd or "ChatGPT", "session": sid}


def _parse_claude_file(path):
    """单个 Claude 会话日志 → [(key, counts, ts, model, session, project)]。"""
    out = []
    keys = ("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")
    try:
        fh = open(path, encoding="utf-8", errors="ignore")
    except OSError:
        return out
    with fh:
        for line in fh:
            if '"usage"' not in line:
                continue
            try:
                r = json.loads(line)
            except ValueError:
                continue
            m = r.get("message") or {}
            u = m.get("usage") or {}
            if r.get("type") != "assistant" or not is_claude(m.get("model")) or not u:
                continue
            counts = _tokens(u, keys)
            ts = _iso_ms(r.get("timestamp"))
            identity = m.get("id") or r.get("requestId")
            # Without a stable request/message identity streaming duplicates
            # cannot be distinguished from distinct requests: do not guess.
            if counts is None or not ts or not identity:
                continue
            out.append(((identity, m["model"]), counts, ts, m["model"],
                        "claude:" + str(r.get("sessionId") or identity),
                        str(r.get("cwd") or "Claude Code")))
    return out


def iter_claude_calls():
    """Claude message usage is cumulative within a streamed message, not per line.

    Keep component maxima for each message/request ID. Cache creation/read are
    disjoint input categories in Claude logs; nested cache breakdowns and a
    possible total_tokens field are never added a second time.
    """
    requests = {}
    for path in sorted(glob.glob(os.path.join(CLAUDE_PROJECTS, "**", "*.jsonl"), recursive=True)):
        for key, counts, ts, model, session, project in _cached_parse(path, _parse_claude_file):
            old = requests.get(key)
            if old:
                old["counts"] = [max(a, b) for a, b in zip(old["counts"], counts)]
                old["ts"] = min(old["ts"], ts)  # attribute to request's first usage
            else:
                requests[key] = {"counts": list(counts), "ts": ts, "model": model,
                                 "session": session, "project": project}
    for r in requests.values():
        inp, out, creation, read = r.pop("counts")
        r.update(when=dt.datetime.fromtimestamp(r["ts"] / 1000), prompt=inp + creation + read,
                 completion=out, total=inp + out + creation + read, cached=read, source=SRC_CLAUDE,
                 # 费用要按组件分别计价（缓存读/写与普通输入不是一个价），
                 # 所以把拆分后的分量也带出去，不要只能从 total 里猜。
                 input=inp, cacheCreation=creation, cacheRead=read)
        yield r


def iter_all_calls():
    yield from iter_calls()
    yield from iter_codex_calls()
    yield from iter_claude_calls()


def today_usage(now=None):
    """All tools independently of the selected animation source, local midnight."""
    now = now or dt.datetime.now().astimezone()
    if now.tzinfo is None:
        now = now.astimezone()
    start = now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp() * 1000
    end = now.timestamp() * 1000
    totals, coverage = {}, {}
    for name, root, reader in (("workbuddy", PROJECTS_DIR, iter_calls),
                               ("codex", CODEX_SESSIONS, iter_codex_calls),
                               ("claude", CLAUDE_PROJECTS, iter_claude_calls)):
        total, count, has_usage = 0, 0, False
        try:
            for r in reader():
                has_usage = True
                if start <= r["ts"] <= end:
                    total += r["total"]
                    count += 1
            status = "connected" if has_usage else "no_trusted_usage" if os.path.isdir(root) else "missing"
        except (OSError, ValueError, TypeError, OverflowError):
            status = "unavailable"
            total, count = 0, 0
        totals[name] = {"totalTokens": total if status == "connected" else None, "calls": count}
        coverage[name] = status
    complete = all(s == "connected" for s in coverage.values())
    return {"totalTokens": sum(v["totalTokens"] or 0 for v in totals.values()),
            "bySource": totals, "coverage": coverage, "complete": complete,
            "note": "已接入三种来源（本机可读日志）" if complete else "仅统计已接入来源",
            "dayLocal": now.date().isoformat(), "timezone": str(now.tzinfo),
            "updatedAt": now.isoformat()}


# ---------------------------------------------------------------- 聚合

def aggregate(records):
    agg = {
        "total": 0, "prompt": 0, "completion": 0, "cached": 0, "calls": 0,
        "by_day": collections.Counter(),
        "by_model": collections.Counter(),
        "by_project": collections.Counter(),
        "by_session": collections.Counter(),
        "model_calls": collections.Counter(),
        # 来源维度（柱状图分色用）
        "by_source": collections.Counter(),
        "source_calls": collections.Counter(),
        "source_prompt": collections.Counter(),
        "source_completion": collections.Counter(),
        "by_day_source": collections.defaultdict(collections.Counter),
        "by_source_model": collections.defaultdict(collections.Counter),
        "project_source": {},
    }
    span = {}
    first = last = None
    for r in records:
        agg["total"] += r["total"]
        agg["prompt"] += r["prompt"]
        agg["completion"] += r["completion"]
        agg["cached"] += r["cached"]
        agg["calls"] += 1
        d = r["when"].strftime("%Y-%m-%d")
        src = r.get("source") or SRC_OTHER
        agg["by_day"][d] += r["total"]
        agg["by_model"][r["model"]] += r["total"]
        agg["model_calls"][r["model"]] += 1
        agg["by_project"][r["project"]] += r["total"]
        agg["by_session"][r["session"]] += r["total"]
        agg["by_source"][src] += r["total"]
        agg["source_calls"][src] += 1
        agg["source_prompt"][src] += r["prompt"]
        agg["source_completion"][src] += r["completion"]
        agg["by_day_source"][d][src] += r["total"]
        agg["by_source_model"][src][r["model"]] += r["total"]
        agg["project_source"][r["project"]] = src
        span.setdefault(r["session"], []).append(r["ts"])
        t = r["when"]
        first = t if first is None else min(first, t)
        last = t if last is None else max(last, t)

    agg["first"], agg["last"] = first, last
    # 最长「一段」连续对话。
    # ⚠️ 不能直接取会话首尾时差：一来 Codex 的会话会被隔几天续写，
    #    首尾时差能到 8 天（那不是"聊了 8 天"）；二来就算同一天，
    #    中间夹着几小时没动也会被算进去。所以按空档切开，
    #    断开超过 IDLE_GAP_MS 就不算同一段。
    #
    # 切好的每一段都留着（agg["runs"]），因为网页上的日期范围要能**就地重算**
    # 这个指标：只要把每段裁到选中区间再取最长，结果和整段重算是同一个数。
    # 反过来，如果只把「会话 × 天」的首末时间塞给前端，天内的大空档就看不见了 ——
    # 实测那样会算出 15.7 小时，而真值是 11.2 小时。
    IDLE_GAP_MS = 2 * 60 * 60 * 1000
    runs = []
    for ts_list in span.values():
        ts_list.sort()
        run_start = prev = ts_list[0]
        for t in ts_list[1:]:
            if t - prev > IDLE_GAP_MS:
                runs.append((run_start, prev))
                run_start = t
            prev = t
        runs.append((run_start, prev))
    agg["runs"] = runs
    if runs:
        best = max((b - a, a, b) for a, b in runs)
        agg["longest_sec"] = best[0] / 1000.0
        agg["longest_from"] = dt.datetime.fromtimestamp(best[1] / 1000).strftime("%m-%d %H:%M")
        agg["longest_to"] = dt.datetime.fromtimestamp(best[2] / 1000).strftime("%m-%d %H:%M")
    else:
        agg["longest_sec"] = 0.0
        agg["longest_from"] = agg["longest_to"] = "-"
    # 日峰值
    if agg["by_day"]:
        peak_day, peak_val = max(agg["by_day"].items(), key=lambda kv: kv[1])
    else:
        peak_day, peak_val = "-", 0
    agg["peak_day"], agg["peak_val"] = peak_day, peak_val
    # 连续天数
    days = sorted({dt.datetime.strptime(d, "%Y-%m-%d").date() for d in agg["by_day"]})
    cur = 0
    if days:
        cur = 1
        for i in range(len(days) - 1, 0, -1):
            if (days[i] - days[i - 1]).days == 1:
                cur += 1
            else:
                break
    best = run = 0
    prev = None
    for d in days:
        run = run + 1 if (prev is not None and (d - prev).days == 1) else 1
        best = max(best, run)
        prev = d
    agg["streak_cur"], agg["streak_best"], agg["active_days"] = cur, best, len(days)
    return agg


# ---------------------------------------------------------------- 格式化

def human(n):
    if n >= 1e8:
        return "%.2f 亿" % (n / 1e8)
    if n >= 1e4:
        return "%.1f 万" % (n / 1e4)
    return "%d" % n


def comma(n):
    return "{:,}".format(int(n))


def fmt_dur(sec):
    sec = int(sec)
    h, m, s = sec // 3600, sec % 3600 // 60, sec % 60
    if h:
        return "%d 小时 %d 分" % (h, m)
    if m:
        return "%d 分 %d 秒" % (m, s)
    return "%d 秒" % s


def print_terminal(agg, title, meta):
    w = 68
    print()
    print("=" * w)
    print("  Token 面板  ·  %s" % title)
    print("=" * w)
    if agg["first"]:
        print("  统计范围：%s ~ %s" % (
            agg["first"].strftime("%Y-%m-%d %H:%M"), agg["last"].strftime("%Y-%m-%d %H:%M")))
    print()
    print("  累计 Token        %16s" % human(agg["total"]))
    print("  峰值 Token        %16s   （%s）" % (human(agg["peak_val"]), agg["peak_day"]))
    print("  最长连续对话      %16s   （断开超 2 小时算另一段）" % fmt_dur(agg["longest_sec"]))
    print("  连续天数          %16s   （当前 %d / 最长 %d，共活跃 %d 天）"
          % ("%d 天" % agg["streak_cur"], agg["streak_cur"], agg["streak_best"], agg["active_days"]))
    print("-" * w)
    print("  调用 %s 次 ｜ 输入 %s ｜ 输出 %s"
          % (comma(agg["calls"]), human(agg["prompt"]), human(agg["completion"])))
    print("=" * w)

    print("\n  按来源")
    for src, v in sorted(agg["by_source"].items(),
                         key=lambda kv: -kv[1]):
        share = (v / agg["total"] * 100) if agg["total"] else 0
        print("  %-12s %14s  %5s 次  %5.1f%%"
              % (src, comma(v), comma(agg["source_calls"][src]), share))

    print("\n  按天用量")
    peak = max(agg["by_day"].values()) if agg["by_day"] else 1
    for day in sorted(agg["by_day"]):
        v = agg["by_day"][day]
        print("  %s  %12s  %s" % (day, comma(v), "█" * max(1, int(v / peak * 30))))

    print("\n  按模型")
    for m, v in agg["by_model"].most_common():
        print("  %-24s %14s  %5s 次" % (m[:24], comma(v), comma(agg["model_calls"][m])))

    print("\n  用量最高的会话（只列前 10，完整看网页报告）")
    for i, (sid, v) in enumerate(agg["by_session"].most_common(10), 1):
        info = meta.get(sid) or {}
        name = info.get("title") or sid[:8]
        print("  %2d. %14s  %s" % (i, comma(v), name[:32]))
    print()


# ---------------------------------------------------------------- HTML 报告

def build_report_data(agg, records, meta):
    """把「整份报告需要的数字」压成一个数据包塞进页面，前端按日期范围就地重算。

    为什么不做成"选范围就让后端重跑"：报告是个本地静态文件，双击打开时
    本地状态服务不一定在跑；一旦依赖它，改范围就会时灵时不灵。
    所以数据包要**足够全**，让任何范围都能纯前端算出来。

    体积实测很小：23 个有量的日子、8 个模型、48 个项目、171 个会话，
    稀疏条目加起来不到 400 条，整包 20KB 出头。

    口径上唯一必须整段带过来的东西是 `runs`（已切好的连续对话分段）——
    见 aggregate() 里的注释，只给"会话 × 天"的首末时间会算错。
    """
    active_sources = [s for s in SOURCE_ORDER if agg["by_source"].get(s)]
    src_idx = {s: i for i, s in enumerate(active_sources)}
    ns = len(active_sources)

    # 连续日期补 0：没用的日子也占一格，时间轴才等距
    days = sorted(agg["by_day"])
    day_list = []
    if days:
        cur = dt.datetime.strptime(days[0], "%Y-%m-%d").date()
        end_day = dt.datetime.strptime(days[-1], "%Y-%m-%d").date()
        while cur <= end_day:
            day_list.append(cur.strftime("%Y-%m-%d"))
            cur += dt.timedelta(days=1)
    di = {d: i for i, d in enumerate(day_list)}

    cell = [[[0, 0, 0, 0] for _ in range(ns)] for _ in range(len(day_list))]
    model_names, model_acc = [], {}
    proj_names, proj_acc = [], {}
    sess_meta, sess_acc = [], {}

    for r in records:
        k = di.get(r["when"].strftime("%Y-%m-%d"))
        si = src_idx.get(r.get("source"))
        if k is None or si is None:
            continue
        c = cell[k][si]
        c[0] += r["total"]
        c[1] += 1
        c[2] += r["prompt"]
        c[3] += r["completion"]

        if r["model"] not in model_acc:
            model_acc[r["model"]] = [len(model_names), {}, 0, 0]
            model_names.append(r["model"])
        slot = model_acc[r["model"]]
        key = (k, si)
        if key not in slot[1]:
            slot[1][key] = [0, 0]
        slot[1][key][0] += r["total"]
        slot[1][key][1] += 1

        if r["project"] not in proj_acc:
            proj_acc[r["project"]] = [len(proj_names), {}, 0]
            proj_names.append(r["project"])
        pslot = proj_acc[r["project"]]
        pslot[1][key] = pslot[1].get(key, 0) + r["total"]

        sid = r["session"]
        if sid not in sess_acc:
            info = meta.get(sid) or {}
            cwd = (info.get("cwd") or "").replace(os.path.expanduser("~"), "~")
            sess_acc[sid] = [len(sess_meta), {}, info.get("title") or ""]
            sess_meta.append([info.get("title") or ("会话 " + sid[:8]), cwd])
        sslot = sess_acc[sid]
        sslot[1][key] = sslot[1].get(key, 0) + r["total"]

    model_cells = []
    for name in model_names:
        mi, cells, _, _ = model_acc[name]
        for (k, si), v in sorted(cells.items()):
            model_cells.append([k, si, mi, v[0], v[1]])
    proj_cells = []
    for name in proj_names:
        pi, cells, _ = proj_acc[name]
        for (k, si), v in sorted(cells.items()):
            proj_cells.append([k, si, pi, v])
    sess_cells = []
    for sid, (sxi, cells, _title) in sess_acc.items():
        for (k, si), v in sorted(cells.items()):
            sess_cells.append([k, sxi, v])

    # 连续对话分段：换算成"相对第一天零点过了多少分钟"，前端裁区间用
    day0_ms = int(dt.datetime.strptime(day_list[0], "%Y-%m-%d").timestamp() * 1000) \
        if day_list else 0
    runs = [[int((a - day0_ms) // 60000), int((b - day0_ms) // 60000)]
            for a, b in agg.get("runs", [])]

    return {
        "sources": active_sources,
        "colors": [SOURCE_COLOR.get(s, "#94a3b8") for s in active_sources],
        "days": day_list,
        # 每天是不是工作日（含调休）。判断放 Python 这边做，前端只读结果 ——
        # 省得两边各写一套假期规则，改一年表要改两个地方。
        "workday": [bool(is_workday(dt.datetime.strptime(d, "%Y-%m-%d").date()))
                    for d in day_list],
        "cell": cell,
        "models": model_names,
        "modelCells": model_cells,
        "projs": proj_names,
        "projCells": proj_cells,
        "sess": sess_meta,
        "sessCells": sess_cells,
        "runs": runs,
        # 自检用：前端把「全部」算完之后跟这两个数对一下，
        # 对不上就在页面上报警 —— 这个文件最怕的就是"两边数字悄悄不一致"
        "check": [agg["total"], agg["calls"]],
    }


def build_html(agg, title, meta, records):
    _built_at = dt.datetime.now()
    generated = _built_at.strftime("%Y-%m-%d %H:%M:%S")
    # 页面要拿它判断"我这份数据是什么时候算的"，用来决定要不要自动重取。
    generated_epoch = int(_built_at.timestamp() * 1000)

    report = build_report_data(agg, records, meta)
    report_json = json.dumps(report, ensure_ascii=False, separators=(",", ":")).replace("<", "\\u003c")

    html_out = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__H1__ · __TITLE__</title>
<style>
  :root{--bg:#f5f6f8;--card:#fff;--ink:#1a1d21;--muted:#6b7280;--line:#e6e8ec;
    --brand:#2b6cff;--brand-soft:#eaf1ff}
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--ink);line-height:1.55;
    font-family:-apple-system,BlinkMacSystemFont,"PingFang SC","Helvetica Neue",Arial,sans-serif;
    -webkit-font-smoothing:antialiased}
  .wrap{max-width:1000px;margin:0 auto;padding:32px 24px 64px}
  h1{font-size:24px;margin:0 0 6px;letter-spacing:-.01em}
  .cards{display:grid;grid-template-columns:repeat(3,1fr);gap:12px;margin:20px 0}
  @media (max-width:720px){.cards{grid-template-columns:1fr 1fr}}
  .card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px 16px}
  .card .k{font-size:12px;color:var(--muted);margin-bottom:4px}
  .card .v{font-size:24px;font-weight:600;letter-spacing:-.02em}
  .card .s{font-size:11px;color:var(--muted);margin-top:2px}
  .card.hi{border-color:#cfe0ff;background:linear-gradient(180deg,#f7faff,#fff)}
  .card.hi .v{color:var(--brand)}  section,.fold{background:var(--card);border:1px solid var(--line);border-radius:12px;
    padding:20px;margin-bottom:16px}
  h2{font-size:15px;margin:0 0 16px;font-weight:600}
  h3.mini{font-size:12px;margin:0 0 12px;font-weight:600;color:var(--muted)}
  /* ---- 图表：连续时间轴 + 按来源堆叠 + 口径切换 ----
     柱子高度用 px 写死（由脚本算），配 transition 做生长动画；
     列宽、间距全部交给 grid，不再用 flex-basis 硬撑。 */
  .chart-head{display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin-bottom:6px}
  .chart-head h2{margin:0}
  /* 日期范围放在页头 —— 它管的是**整页**的数字（卡片、各张表都跟着变），
     放在图表标题旁边会让人以为只影响图表。
     原来是 <select>，选项写"10-05 ~ 10-11 那一周"这种话没人看得懂，改成点日历。 */
  .range-wrap{position:relative}
  .range-btn{font:inherit;font-size:12.5px;font-weight:500;color:var(--ink);background:#fff;
    border:1px solid var(--line);border-radius:8px;padding:6px 11px;cursor:pointer;
    display:inline-flex;align-items:center;gap:8px;
    transition:background .15s,border-color .15s,color .15s}
  .range-btn:hover{background:#f7f8fa;border-color:#c9cdd4}
  .range-btn:focus-visible{outline:2px solid #cfe0ff;outline-offset:1px}
  .range-btn.active{background:var(--brand-soft);border-color:#bfd6ff;color:#1d4ed8}
  .range-btn .caret{width:0;height:0;border-left:4px solid transparent;
    border-right:4px solid transparent;border-top:5px solid currentColor;opacity:.55}
  .range-pop{position:absolute;z-index:40;top:calc(100% + 6px);left:0;width:330px;
    background:#fff;border:1px solid var(--line);border-radius:12px;
    box-shadow:0 10px 34px rgba(16,24,40,.14);padding:10px;user-select:none}
  /* 预设**必须挤在一行**里（换行很难看），所以弹层加宽、按钮收紧 */
  .rp-presets{display:flex;gap:5px;flex-wrap:nowrap;margin-bottom:8px}
  .rp-presets button{font:inherit;font-size:11px;color:var(--muted);background:#f5f6f8;
    border:0;border-radius:6px;padding:4px 8px;cursor:pointer;white-space:nowrap}
  .rp-presets button:hover{background:#eaeef3;color:var(--ink)}
  .rp-presets button.on{background:var(--brand-soft);color:#1d4ed8;font-weight:600}
  .rp-head{display:flex;align-items:center;justify-content:space-between;margin:2px 0 7px}
  .rp-head .rp-t{font-size:12.5px;font-weight:600}
  .rp-nav{font:inherit;font-size:14px;line-height:1;color:var(--muted);background:#fff;
    border:1px solid var(--line);border-radius:6px;width:24px;height:24px;cursor:pointer}
  .rp-nav:hover{background:#f7f8fa;color:var(--ink)}
  .rp-nav:disabled{opacity:.35;cursor:default}
  .rp-wd{display:grid;grid-template-columns:repeat(7,1fr);gap:2px;margin-bottom:2px}
  .rp-wd span{font-size:10.5px;color:var(--muted);text-align:center;line-height:17px}
  .rp-grid{display:grid;grid-template-columns:repeat(7,1fr);gap:2px}
  .rp-grid button{font:inherit;font-size:11.5px;height:29px;border:0;background:transparent;
    border-radius:7px;cursor:pointer;color:var(--ink);font-variant-numeric:tabular-nums}
  .rp-grid button:hover{background:#eef3ff}
  .rp-grid button.off{color:#c8ccd3;cursor:default;background:transparent}
  /* 开了「工作日」时，不参与统计的日子标灰 —— 用户问过"33 天里哪几天算工作日" */
  .rp-grid button.rest{color:#c3c8d2;text-decoration:line-through}
  .rp-grid button.today{box-shadow:inset 0 0 0 1px #cfe0ff}
  .rp-grid button.edge{background:var(--brand);color:#fff}
  .rp-grid button.in{background:#eaf1ff;color:#1d4ed8}
  .rp-grid button.blank{visibility:hidden;cursor:default}
  .rp-foot{display:flex;align-items:center;justify-content:space-between;margin-top:8px;
    padding-top:8px;border-top:1px solid var(--line);font-size:11.5px;color:var(--muted)}
  .rp-clear{font:inherit;font-size:11.5px;color:var(--brand);background:0;border:0;cursor:pointer}
  .chart{position:relative;display:grid;grid-auto-flow:column;
    grid-auto-columns:minmax(16px,1fr);gap:4px;height:234px;overflow-x:auto;
    padding:8px 16px 0;align-items:stretch}
  /* 左右各留 16px：最边上那列的数值标签比列宽还宽（"8479万"要 30px、列只有 25px），
     不留白就会被 overflow 裁掉半个字 —— 实测最左边那个 "8" 就是这么没的。 */
  /* 基线：给柱脚一条实线。没有它的时候，几根很小的柱子看着像悬空的横线。 */
  .baseline{position:absolute;left:16px;right:16px;bottom:22px;height:1px;
    background:var(--line);pointer-events:none}
  .bar-col{display:flex;flex-direction:column;align-items:center;justify-content:flex-end;
    border-radius:6px;transition:background .15s}
  .bar-col:hover{background:#f5f8ff}
  .bar-val{font-size:10px;line-height:14px;height:14px;color:var(--muted);
    margin-bottom:4px;white-space:nowrap;font-variant-numeric:tabular-nums}
  /* 柱子被点选时：只给柱子本身描一圈 + 日期变蓝。
     不要给整列上底色 —— 那会在高柱子上方多出一个方框，看着像画错了。 */
  .bar-col.picked .bar{outline:2px solid var(--brand);outline-offset:3px}
  .bar-col.picked .bar-day{color:var(--brand);font-weight:600}
  /* 点柱子之后出现的读数条：**横着写**，一次看全，不用扭脖子 */
  .chart-pick{margin-top:12px;padding:9px 12px;border:1px solid #dbe7ff;background:#f6faff;
    border-radius:9px;font-size:12.5px;display:flex;flex-wrap:wrap;align-items:center;gap:6px 16px}
  .chart-pick b{font-weight:600;font-variant-numeric:tabular-nums}
  .chart-pick .pk-date{font-weight:600}
  .chart-pick .pk-dim{color:var(--muted);font-size:11.5px;font-variant-numeric:tabular-nums}
  .chart-pick i{display:inline-block;width:8px;height:8px;border-radius:2px;
    margin-right:6px;vertical-align:-1px}
  /* max-width 是给「按月」「按星期」用的：那时只有 2~7 根柱子，
     列宽会被 1fr 拉到 400+px，不封顶就变成一块块巨型色板。
     52px 是"看着像柱状图"而不是"色块"的上限。 */
  .bar{width:100%;max-width:52px;display:flex;flex-direction:column;justify-content:flex-end;
    border-radius:4px 4px 0 0;overflow:hidden;min-height:2px;
    transition:height .45s cubic-bezier(.22,.61,.36,1)}
  .seg{width:100%}
  .bar-day{font-size:10px;line-height:16px;height:16px;color:var(--muted);
    margin-top:6px;white-space:nowrap}
  .bar-col:hover .bar-day{color:var(--ink)}
  .chart-foot{display:flex;align-items:center;gap:14px;flex-wrap:wrap;margin-top:10px}
  .axis-note{font-size:11px;color:var(--muted)}
  .chart-legend{display:flex;gap:16px;align-items:center;font-size:11px;
    color:var(--muted);margin-left:auto}
  .chart-legend i{display:inline-block;width:9px;height:9px;border-radius:2px;
    margin-right:5px;vertical-align:-1px}
  #chartTip{position:fixed;z-index:99;pointer-events:none;opacity:0;
    transition:opacity .12s;background:#1f2937;color:#fff;border-radius:8px;
    padding:8px 10px;font-size:11.5px;line-height:1.6;box-shadow:0 6px 22px rgba(0,0,0,.22);
    white-space:nowrap}
  #chartTip.on{opacity:1}
  #chartTip .t{font-weight:600;font-size:12px;margin-bottom:3px}
  #chartTip .r{display:flex;align-items:center;gap:6px}
  #chartTip .r i{display:inline-block;width:7px;height:7px;border-radius:2px}
  #chartTip .tot{border-top:1px solid rgba(255,255,255,.22);margin-top:4px;padding-top:4px;
    font-weight:600}
  .dot{display:inline-block;width:8px;height:8px;border-radius:2px;margin-right:6px}
  .src-row td{font-weight:600;color:var(--muted);font-size:12px;padding-top:14px}
  table{width:100%;border-collapse:collapse;font-size:13px}
  th{text-align:left;color:var(--muted);font-weight:500;font-size:12px;
    border-bottom:1px solid var(--line);padding:0 8px 8px}
  td{padding:9px 8px;border-bottom:1px solid #f1f2f4;vertical-align:top}
  tr:last-child td{border-bottom:none}
  .num{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}
  .rk{color:var(--muted);width:28px}
  .sub{color:var(--muted);font-size:11px;margin-top:2px;word-break:break-all}
  .cols{display:grid;grid-template-columns:1fr 1fr;gap:16px}
  @media (max-width:760px){.cols{grid-template-columns:1fr}}
  details.fold{padding:0}
  details.fold>summary{cursor:pointer;padding:16px 20px;font-size:14px;font-weight:600;
    list-style:none;display:flex;align-items:center;justify-content:space-between;
    user-select:none;border-radius:12px}
  details.fold>summary::-webkit-details-marker{display:none}
  details.fold>summary:hover{background:#fafbfc}
  details.fold>summary .hint{font-size:12px;color:var(--muted);font-weight:400}
  details.fold>summary .hint::after{content:" ▾"}
  details.fold[open]>summary{border-bottom:1px solid var(--line);border-radius:12px 12px 0 0}
  details.fold[open]>summary .hint::after{content:" ▴"}
  .fold-body{padding:16px 20px 20px}
  footer{color:var(--muted);font-size:12px;text-align:center;margin-top:24px}
  /* 「重新统计」按钮：报告是快照，不重写就永远停在生成那一刻。
     用户要求**只给一个按钮就够了** —— 生成时间上面那行 meta 已经写了，
     不要再拿黄底提示条占地方。失败时才在下方补一行说明。 */
  .head-row{display:flex;align-items:center;gap:14px;flex-wrap:wrap}
  .head-row h1{margin:0}
  #regenBtn{margin-left:auto;font:inherit;font-size:13px;font-weight:600;cursor:pointer;
    color:#1f2937;background:#fff;border:1px solid var(--line);border-radius:8px;
    padding:6px 14px;transition:background .15s,border-color .15s}
  #regenBtn:hover{background:#f7f8fa;border-color:#c9cdd4}
  #regenBtn:disabled{opacity:.6;cursor:default}
  .regen-note{margin-top:8px;font-size:12.5px;color:#b45309}
  .warn-note{margin-top:8px;font-size:12.5px;color:#b91c1c}
</style>
</head>
<body>
<div class="wrap">
  <header>
    <div class="head-row">
      <h1>__H1__</h1>
      <div class="range-wrap">
        <button id="rangeBtn" class="range-btn" type="button" aria-haspopup="dialog">
          <span id="rangeText">全部 __NDAYS__ 天</span><span class="caret"></span>
        </button>
        <div id="rangePop" class="range-pop" hidden>
          <div class="rp-presets" id="rpPresets">
            <button type="button" data-p="7">近 7 天</button>
            <button type="button" data-p="30">近 30 天</button>
            <button type="button" data-p="month">本月</button>
            <button type="button" data-p="work" id="rpWork" title="只统计工作日：跳过周末和法定假日；调休上班的周末算工作日（按国务院办公厅 2026 年放假安排）">工作日</button>
            <button type="button" data-p="all">全部</button>
          </div>
          <div class="rp-head">
            <button type="button" class="rp-nav" id="rpPrev" aria-label="上个月">‹</button>
            <span class="rp-t" id="rpTitle"></span>
            <button type="button" class="rp-nav" id="rpNext" aria-label="下个月">›</button>
          </div>
          <div class="rp-wd">
            <span>一</span><span>二</span><span>三</span><span>四</span>
            <span>五</span><span>六</span><span>日</span>
          </div>
          <div class="rp-grid" id="rpGrid"></div>
          <div class="rp-foot">
            <span id="rpSel"></span>
            <button type="button" class="rp-clear" id="rpClear">清空</button>
          </div>
        </div>
      </div>
      <button id="regenBtn" type="button" data-gen="__GENEPOCH__"
        title="重新扫描本地记录并刷新本页　·　本次数据生成于 __GEN__">重新统计</button>
    </div>
    <div class="regen-note" id="regenNote" hidden></div>
    <div class="warn-note" id="warnNote" hidden></div>
  </header>

  <div class="cards" id="cards"></div>

  <section>
    <h2>Token 活动（按天）</h2>
    <div class="chart" id="chart"><div class="baseline"></div></div>
    <div class="chart-pick" id="chartPick" hidden></div>
    <div class="chart-foot">
      <span class="axis-note" id="axisNote"></span>
      <div class="chart-legend" id="legend"></div>
    </div>
  </section>

  <section>
    <h2>按来源</h2>
    <table>
      <thead><tr><th>来源</th><th class="num">调用</th><th class="num">输入</th>
        <th class="num">输出</th><th class="num">token</th><th class="num">占比</th></tr></thead>
      <tbody id="srcRows"></tbody>
    </table>
  </section>

  <section>
    <h2>按模型</h2>
    <table>
      <thead><tr><th>模型</th><th class="num">调用</th><th class="num">token</th><th class="num">约</th></tr></thead>
      <tbody id="modelRows"></tbody>
    </table>
  </section>

  <details class="fold">
    <summary>用量最高的会话<span class="hint" id="sessHint"></span></summary>
    <div class="fold-body">
      <table>
        <thead><tr><th></th><th>会话</th><th class="num">token</th><th class="num">约</th></tr></thead>
        <tbody id="sessRows"></tbody>
      </table>
    </div>
  </details>

  <details class="fold">
    <summary>按天明细　·　按项目<span class="hint">明细</span></summary>
    <div class="fold-body">
      <h3 class="mini">按天明细</h3>
      <table>
        <thead id="dayHead"><tr><th>日期</th><th class="num">合计</th></tr></thead>
        <tbody id="dayRows"></tbody>
      </table>
      <h3 class="mini" style="margin:22px 0 12px">按项目</h3>
      <table>
        <thead><tr><th>来源</th><th>项目目录</th><th class="num">token</th>
          <th class="num">约</th></tr></thead>
        <tbody id="projRows"></tbody>
      </table>
    </div>
  </details>

  <footer>数据来源：本机 AI 工具的会话记录　·　ChatGPT（<code>~/.codex/sessions</code>）＋
    DeepSeek（WorkBuddy，<code>~/.workbuddy/projects</code>）<br>
    口径一致：token = 输入 + 输出，缓存重读计入输入，与接口账单口径相同，两个来源可直接相加对比</footer>
</div>

<script id="chartData" type="application/json">__CHART_JSON__</script>
<div id="chartTip"></div>
<script>
/* Token 面板的全部渲染都在这里。
   数据包在 #chartData 里，所以换日期范围是**纯前端重算，零延迟**，
   不用回头重跑统计、也不用联网；双击 html 打开就能用。

   为什么数据要带这么全（每天每来源、模型/项目/会话的按天稀疏表、以及
   已经切好的连续对话分段）：范围一旦只作用于图表、上面的数字不动，
   看起来就像坏了。所以选了范围，卡片和下面所有表都得跟着变。
   唯一不能现算的是"最长连续对话"——它依赖每次调用的秒级时间戳，
   所以后端把切好的分段（runs）整段带过来，前端只做裁剪。

   日期范围是日历选起止；纵轴按当前范围的峰值换算，量程写在左下角。 */
(function () {
  var dataEl = document.getElementById('chartData');
  if (!dataEl) return;
  var D = JSON.parse(dataEl.textContent);
  var SRC = D.sources || [], COLORS = D.colors || [], DAYS = D.days || [];
  var CELL = D.cell || [], MODELS = D.models || [], MODELCELLS = D.modelCells || [];
  var PROJS = D.projs || [], PROJCELLS = D.projCells || [];
  var SESS = D.sess || [], SESSCELLS = D.sessCells || [], RUNS = D.runs || [];
  var WORK = D.workday || [];      /* 每天是不是工作日（Python 侧算好的，含调休）*/
  if (!DAYS.length) { var ax = document.getElementById('axisNote'); if (ax) ax.textContent = '暂无数据'; return; }

  var MAXH = 150, FLOOR = 4, NS = SRC.length;
  var MINDAY = DAYS[0], MAXDAY = DAYS[DAYS.length - 1];
  var DAY0 = ep(DAYS[0]);
  var FROM = null, TO = null, anchor = null;
  var onlyWork = false;   /* 只看工作日（跳过周末/法定假日，调休上班的算工作日）*/
  var picked = -1;        /* 点选中的柱子（在当前范围里的下标）*/

  /* 从地址栏的 # 里把范围恢复回来。
     为什么要：点「重新统计」会 reload，不记住的话用户刚圈好的"本周"就被打回
     "全部"，等于每刷新一次就要重选一遍。
     存 hash 而不是 localStorage：reload 会天然带上它，而关掉页面重新双击打开
     时又回到"全部"——那才是干净的默认值。 */
  (function restoreRange() {
    var h = (location.hash || '').replace(/^#/, '');
    if (!h) return;
    if (h.indexOf('!w') >= 0) { onlyWork = true; h = h.split('!')[0]; }
    if (!h || h === 'all') return;
    var p = h.split('~');
    if (p.length !== 2) return;
    var a = p[0], b = p[1];
    if (a.length !== 10 || b.length !== 10) return;
    if (a.charAt(4) !== '-' || b.charAt(4) !== '-') return;
    if (a < MINDAY) a = MINDAY;          /* 数据变了就把范围夹回有效区间 */
    if (b > MAXDAY) b = MAXDAY;
    if (a > b) return;
    FROM = a; TO = b;
  })();

  function writeHash() {
    var want = '#' + ((FROM && TO) ? (FROM + '~' + TO) : 'all') + (onlyWork ? '!w' : '');
    if (location.hash === want) return;
    try {
      history.replaceState(null, '', want);   /* file:// 下不一定允许，失败就退回改 hash */
    } catch (e) {
      location.hash = want;
    }
  }
  var calY = +MAXDAY.slice(0, 4), calM = +MAXDAY.slice(5, 7) - 1;

  var chart = document.getElementById('chart');
  var legend = document.getElementById('legend');
  var axisNote = document.getElementById('axisNote');
  var tip = document.getElementById('chartTip');
  var pickEl = document.getElementById('chartPick');
  var cardsEl = document.getElementById('cards');
  var srcRows = document.getElementById('srcRows');
  var modelRows = document.getElementById('modelRows');
  var sessRows = document.getElementById('sessRows');
  var sessHint = document.getElementById('sessHint');
  var dayHead = document.getElementById('dayHead');
  var dayRows = document.getElementById('dayRows');
  var projRows = document.getElementById('projRows');
  var rangeBtn = document.getElementById('rangeBtn');
  var rangeText = document.getElementById('rangeText');
  var rangePop = document.getElementById('rangePop');
  var rangeWrap = rangeBtn.parentNode;
  var warnNote = document.getElementById('warnNote');
  var rpGrid = document.getElementById('rpGrid');
  var rpTitle = document.getElementById('rpTitle');
  var rpPrev = document.getElementById('rpPrev');
  var rpNext = document.getElementById('rpNext');
  var rpSel = document.getElementById('rpSel');
  var rpPresets = document.getElementById('rpPresets');
  var rpClear = document.getElementById('rpClear');

  /* ------------------------------------------------ 小工具 */

  function od(n) { return n < 10 ? '0' + n : '' + n; }
  function ep(s) { var p = s.split('-'); return Date.UTC(+p[0], +p[1] - 1, +p[2]); }
  function dstr(y, m, d) { return y + '-' + od(m + 1) + '-' + od(d); }
  function addDays(s, n) { return new Date(ep(s) + n * 86400000).toISOString().slice(0, 10); }
  function zeros(n) { var a = [], i; for (i = 0; i < n; i++) a.push(0); return a; }
  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"]/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c];
    });
  }
  function num(n) {                       /* 千分位，不用正则，省得和 Python 转义打架 */
    var s = '' + n, o = '', c = 0, i;
    for (i = s.length - 1; i >= 0; i--) { o = s.charAt(i) + o; if (++c % 3 === 0 && i > 0) o = ',' + o; }
    return o;
  }
  function human(n) {                     /* 和 Python 的 human() 同一套口径 */
    if (n >= 1e8) return (n / 1e8).toFixed(2) + ' 亿';
    if (n >= 1e4) return (n / 1e4).toFixed(1) + ' 万';
    return '' + n;
  }
  function shortNum(n) {
    if (n >= 1e8) return (n / 1e8).toFixed(1) + '亿';
    if (n >= 1e4) return Math.round(n / 1e4) + '万';
    return '' + n;
  }
  function dur(sec) {                     /* 和 Python 的 fmt_dur() 同一套口径 */
    sec = Math.round(sec);
    var h = Math.floor(sec / 3600), m = Math.floor(sec % 3600 / 60), s = sec % 60;
    if (h) return h + ' 小时 ' + m + ' 分';
    if (m) return m + ' 分 ' + s + ' 秒';
    return s + ' 秒';
  }
  function stamp(absMin) {                /* 绝对分钟 → "09-23 10:19" */
    var d = new Date(DAY0 + absMin * 60000);
    return od(d.getUTCMonth() + 1) + '-' + od(d.getUTCDate()) + ' '
         + od(d.getUTCHours()) + ':' + od(d.getUTCMinutes());
  }
  var TODAY = (function () { var t = new Date(); return dstr(t.getFullYear(), t.getMonth(), t.getDate()); })();

  /* ------------------------------------------------ 范围 */

  function inRange(k) { return (!FROM || k >= FROM) && (!TO || k <= TO); }
  function selectedIdx() {
    var out = [], i;
    for (i = 0; i < DAYS.length; i++) {
      if (!inRange(DAYS[i])) continue;
      if (onlyWork && !WORK[i]) continue;
      out.push(i);
    }
    return out;
  }
  /* 当前实际统计了多少天（开了工作日就只算工作日）。
     用户原话："33 天中的工作日是哪几天我怎么不知道" —— 所以这个数得摆在明面上。 */
  function selectedCount() {
    var c = 0, i;
    for (i = 0; i < DAYS.length; i++) {
      if (!inRange(DAYS[i])) continue;
      if (onlyWork && !WORK[i]) continue;
      c++;
    }
    return c;
  }

  function rangeLabel() {
    var base = (!FROM && !TO) ? ('全部 ' + DAYS.length + ' 天')
                              : ((FROM || MINDAY).slice(5) + ' ~ ' + (TO || MAXDAY).slice(5));
    return onlyWork ? (base + ' · 工作日 ' + selectedCount() + ' 天') : base;
  }

  /* ------------------------------------------------ 聚合（按当前范围） */

  function compute(sel) {
    var pos = {}, a, i, si, k;
    for (a = 0; a < sel.length; a++) pos[sel[a]] = a;

    var byDay = [], byDaySrc = [], sTok = zeros(NS), sCalls = zeros(NS),
        sPrompt = zeros(NS), sCompl = zeros(NS);
    var total = 0, calls = 0, peak = 0, peakIdx = -1, active = [];

    for (a = 0; a < sel.length; a++) {
      k = sel[a];
      var row = CELL[k] || [], tot = 0, per = zeros(NS);
      for (si = 0; si < NS; si++) {
        var c = row[si] || [0, 0, 0, 0];
        tot += c[0]; per[si] = c[0];
        sTok[si] += c[0]; sCalls[si] += c[1];
        sPrompt[si] += c[2]; sCompl[si] += c[3];
        calls += c[1];
      }
      byDay.push(tot); byDaySrc.push(per); total += tot;
      if (tot > peak) { peak = tot; peakIdx = a; }
      if (tot > 0) active.push(a);
    }

    /* 连续天数按**自然日**判断相邻，不能看 sel 的下标：
       开了「工作日」之后周五和周一在 sel 里是挨着的，中间却隔了两天。 */
    function nextDay(a, b) { return ep(DAYS[sel[b]]) - ep(DAYS[sel[a]]) === 86400000; }
    var bestStreak = 0, curStreak = 0, prev = -1;
    active.forEach(function (x) {
      curStreak = (prev >= 0 && nextDay(prev, x)) ? curStreak + 1 : 1;
      if (curStreak > bestStreak) bestStreak = curStreak;
      prev = x;
    });
    var tail = 0;
    for (i = active.length - 1; i >= 0; i--) {
      if (i === active.length - 1 || nextDay(active[i], active[i + 1])) tail++;
      else break;
    }

    var mTok = zeros(MODELS.length), mCalls = zeros(MODELS.length),
        mSrc = [], psTok = zeros(PROJS.length), psSrc = [], ssTok = zeros(SESS.length);
    for (i = 0; i < MODELS.length; i++) mSrc.push(-1);
    for (i = 0; i < PROJS.length; i++) psSrc.push(-1);
    MODELCELLS.forEach(function (r) {
      if (pos[r[0]] === undefined) return;
      mTok[r[2]] += r[3]; mCalls[r[2]] += r[4];
      if (mSrc[r[2]] < 0) mSrc[r[2]] = r[1];
    });
    PROJCELLS.forEach(function (r) {
      if (pos[r[0]] === undefined) return;
      psTok[r[2]] += r[3];
      if (psSrc[r[2]] < 0) psSrc[r[2]] = r[1];
    });
    SESSCELLS.forEach(function (r) {
      if (pos[r[0]] === undefined) return;
      ssTok[r[1]] += r[2];
    });

    /* 最长连续对话：把服务端切好的分段裁到当前范围，取最长的一段。
       不能自己按"会话 × 天"的首末时间去合并 —— 天内的大空档看不见。 */
    /* 先按连续日期把 sel 切成块（开了「工作日」就不是一整段），
       再把每段对话裁到块里取最长。这样周末断开的两截不会被接在一起。
       没开过滤时只有一块，结果和不切完全一样。 */
    var blocks = [], bi = 0;
    while (bi < sel.length) {
      var bj = bi;
      while (bj + 1 < sel.length && sel[bj + 1] === sel[bj] + 1) bj++;
      blocks.push([sel[bi] * 1440, sel[bj] * 1440 + 1439]);
      bi = bj + 1;
    }
    var bestSec = 0, bestA = 0, bestB = 0;
    RUNS.forEach(function (r) {
      for (var q = 0; q < blocks.length; q++) {
        var pa = Math.max(r[0], blocks[q][0]), pb = Math.min(r[1], blocks[q][1]);
        if (pb > pa && (pb - pa) * 60 > bestSec) {
          bestSec = (pb - pa) * 60; bestA = pa; bestB = pb;
        }
      }
    });

    return {
      sel: sel, byDay: byDay, byDaySrc: byDaySrc, total: total, calls: calls,
      peak: peak, peakDay: peakIdx >= 0 ? DAYS[sel[peakIdx]] : '-',
      activeDays: active.length, bestStreak: bestStreak, curStreak: tail,
      sTok: sTok, sCalls: sCalls, sPrompt: sPrompt, sCompl: sCompl,
      mTok: mTok, mCalls: mCalls, mSrc: mSrc,
      psTok: psTok, psSrc: psSrc, ssTok: ssTok,
      bestSec: bestSec, durFrom: bestSec ? stamp(bestA) : '-', durTo: bestSec ? stamp(bestB) : '-'
    };
  }

  /* ------------------------------------------------ 渲染 */

  function renderCards(A) {
    cardsEl.innerHTML =
      '<div class="card hi"><div class="k">累计 Token</div><div class="v">' + human(A.total) +
        '</div><div class="s">' + num(A.total) + '</div></div>' +
      '<div class="card"><div class="k">峰值 Token</div><div class="v">' + human(A.peak) +
        '</div><div class="s">' + esc(A.peakDay) + '</div></div>' +
      '<div class="card"><div class="k">最长连续对话</div><div class="v">' + dur(A.bestSec) +
        '</div><div class="s">' + esc(A.durFrom) + ' → ' + esc(A.durTo) +
        '　·　断开超 2 小时另算</div></div>' +
      '<div class="card"><div class="k">当前连续天数</div><div class="v">' + A.curStreak +
        ' 天</div><div class="s">最长 ' + A.bestStreak + ' 天　共活跃 ' + A.activeDays + ' 天</div></div>' +
      '<div class="card"><div class="k">调用次数</div><div class="v">' + num(A.calls) +
        '</div><div class="s">次 LLM 请求</div></div>';
  }

  /* 柱顶数字放不放得下？用真实字宽量，别用固定阈值：
     同一页里 "1.2亿" 只有 24px，而 "9454万" 要 36px，差 50%。
     宽度按最长的那条算，再加 4px 缝，才是真正不会压字的条件。 */
  var measEl = null;
  function labelsFit(values, colW) {
    if (!measEl) {
      measEl = document.createElement('span');
      measEl.style.cssText = 'position:absolute;left:-9999px;top:0;white-space:nowrap;'
                           + 'font-size:10px;font-variant-numeric:tabular-nums';
      document.body.appendChild(measEl);
    }
    var maxW = 0, i;
    for (i = 0; i < values.length; i++) {
      measEl.textContent = shortNum(values[i]);
      if (measEl.offsetWidth > maxW) maxW = measEl.offsetWidth;
    }
    return colW >= maxW + 4;
  }

  function renderChart(A) {
    var sel = A.sel, n = sel.length, peak = 0, i, j;
    A.byDay.forEach(function (t) { if (t > peak) peak = t; });

    /* 柱顶数字的显示规则：
       —— 只有**放得下的时候**才横着标（比如一周 7 根，每列一百多 px）。
       —— 放不下就一个都不标，绝不抽稀、绝不竖排。
          抽稀看着像漏数据（用户报过 bug）；竖排要歪着脖子看（用户骂过丑）。
          密集的时候看明确数字的入口是**点柱子**，见下面的 chartPick。
       —— 判断用实测字宽，不用固定阈值：标签本身有长有短（"1.2亿" 24px、
          "9454万" 36px），按最长的那条算才不会压字。 */
    var colW = (chart.clientWidth || 900) / Math.max(n, 1);
    var dayStep = Math.max(1, Math.ceil(31 / colW));
    var showVals = labelsFit(A.byDay, colW);

    Array.prototype.forEach.call(chart.querySelectorAll('.bar-col'), function (e) {
      e.parentNode.removeChild(e);
    });

    var frag = document.createDocumentFragment(), plan = [], floored = 0;
    for (i = 0; i < n; i++) {
      var total = A.byDay[i], per = A.byDaySrc[i], raw, h, inner = '';
      if (total > 0) {
        raw = total / (peak || 1) * MAXH;
        h = Math.max(FLOOR, Math.round(raw));
        if (raw < FLOOR) floored++;
        var parts = [];
        for (j = 0; j < NS; j++) if (per[j]) parts.push({ c: COLORS[j] || '#94a3b8', p: per[j] / total * 100 });
        if (parts.length === 1) {
          /* 只有一段：圆角交给 .bar 自己裁剪，别在这里再加，否则接缝处会露出背景 */
          inner = '<div class="seg" style="height:100%;background:' + parts[0].c + '"></div>';
        } else {
          inner = parts.map(function (p) {
            return '<div class="seg" style="height:' + p.p.toFixed(3) + '%;background:' + p.c + '"></div>';
          }).join('');
        }
      } else {
        h = 2;
      }
      var showDay = (i % dayStep === 0) || i === n - 1;
      var col = document.createElement('div');
      col.className = 'bar-col' + (total > 0 ? '' : ' zero') + (i === picked ? ' picked' : '');
      col.innerHTML = '<div class="bar-val">' + (showVals ? shortNum(total) : '') + '</div>'
                    + '<div class="bar" style="height:' + (total > 0 ? 0 : 2) + 'px;'
                    + (total > 0 ? '' : 'background:#eef1f5') + '">' + inner + '</div>'
                    + '<div class="bar-day">' + (showDay ? DAYS[sel[i]].slice(5) : '') + '</div>';
      col.setAttribute('data-i', i);
      frag.appendChild(col);
      plan.push([col, h, total > 0]);
    }
    chart.appendChild(frag);
    /* 生长动画：先 0 高，下一帧再给真实高度，逐列错开 —— 33 根一起动会糊成一片 */
    requestAnimationFrame(function () {
      plan.forEach(function (it, n2) {
        setTimeout(function () {
          if (!it[2]) return;
          var bar = it[0].querySelector('.bar');
          if (bar) bar.style.height = it[1] + 'px';
        }, Math.min(n2 * 9, 240));
      });
    });

    var lgd = [], si;
    for (si = 0; si < NS; si++) if (A.sTok[si] > 0) lgd.push(si);
    if (!lgd.length) for (si = 0; si < NS; si++) lgd.push(si);
    legend.innerHTML = lgd.map(function (si) {
      return '<span><i style="background:' + COLORS[si] + '"></i>' + esc(SRC[si]) + '</span>';
    }).join('');
    var note = peak ? '纵轴上限 ' + shortNum(peak) + '（当前范围峰值）' : '暂无数据';
    if (floored) note += '　·　' + floored + ' 根太小，按 ' + FLOOR + 'px 画';
    axisNote.textContent = note;
  }

  function renderSources(A) {
    var rows = [];
    for (var si = 0; si < NS; si++) {
      if (A.sTok[si] > 0) rows.push({ si: si, v: A.sTok[si] });   /* 这个范围里没量的来源不占行 */
    }
    rows.sort(function (a, b) { return b.v - a.v; });
    srcRows.innerHTML = rows.map(function (r) {
      return '<tr><td><span class="dot" style="background:' + COLORS[r.si] + '"></span>'
        + esc(SRC[r.si]) + '</td><td class="num">' + num(A.sCalls[r.si])
        + '</td><td class="num">' + human(A.sPrompt[r.si])
        + '</td><td class="num">' + human(A.sCompl[r.si])
        + '</td><td class="num">' + num(r.v)
        + '</td><td class="num">' + (A.total ? (r.v / A.total * 100).toFixed(1) + '%' : '-')
        + '</td></tr>';
    }).join('') || '<tr><td colspan="6">这个范围里没有用量</td></tr>';
  }

  function renderModels(A) {
    var html = '', si, i;
    for (si = 0; si < NS; si++) {
      var idx = [];
      for (i = 0; i < MODELS.length; i++) if (A.mSrc[i] === si && A.mTok[i] > 0) idx.push(i);
      if (!idx.length) continue;
      idx.sort(function (a, b) { return A.mTok[b] - A.mTok[a]; });
      html += '<tr class="src-row"><td colspan="4"><span class="dot" style="background:'
            + COLORS[si] + '"></span>' + esc(SRC[si]) + '</td></tr>';
      idx.forEach(function (m) {
        html += '<tr><td>' + esc(MODELS[m]) + '</td><td class="num">' + num(A.mCalls[m])
              + '</td><td class="num">' + num(A.mTok[m]) + '</td><td class="num">'
              + human(A.mTok[m]) + '</td></tr>';
      });
    }
    modelRows.innerHTML = html || '<tr><td colspan="4">这个范围里没有用量</td></tr>';
  }

  function renderSessions(A) {
    var idx = [], i;
    for (i = 0; i < SESS.length; i++) if (A.ssTok[i] > 0) idx.push(i);
    idx.sort(function (a, b) { return A.ssTok[b] - A.ssTok[a]; });
    var top = idx.slice(0, 200);
    sessRows.innerHTML = top.map(function (m, n) {
      var info = SESS[m] || ['', ''];
      return '<tr><td class="rk">' + (n + 1) + '</td><td>' + esc(info[0])
        + '<div class="sub">' + esc(info[1]) + '</div></td>'
        + '<td class="num">' + num(A.ssTok[m]) + '</td><td class="num">' + human(A.ssTok[m])
        + '</td></tr>';
    }).join('') || '<tr><td colspan="4">这个范围里没有会话记录</td></tr>';
    sessHint.textContent = '共 ' + idx.length + ' 个';
  }

  function renderDays(A) {
    var heads = '<tr><th>日期</th>';
    for (var si = 0; si < NS; si++) heads += '<th class="num">' + esc(SRC[si]) + '</th>';
    heads += '<th class="num">合计</th></tr>';
    dayHead.innerHTML = heads;

    dayRows.innerHTML = A.sel.map(function (k, i) {
      var cells = '';
      for (var si = 0; si < NS; si++) cells += '<td class="num">' + num(A.byDaySrc[i][si]) + '</td>';
      return '<tr><td>' + DAYS[k] + '</td>' + cells + '<td class="num">' + num(A.byDay[i]) + '</td></tr>';
    }).join('');
  }

  function renderProjects(A) {
    var idx = [], i;
    for (i = 0; i < PROJS.length; i++) if (A.psTok[i] > 0) idx.push(i);
    idx.sort(function (a, b) { return A.psTok[b] - A.psTok[a]; });
    projRows.innerHTML = idx.map(function (m) {
      var si = A.psSrc[m] < 0 ? 0 : A.psSrc[m];
      return '<tr><td><span class="dot" style="background:' + COLORS[si] + '"></span>'
        + esc(SRC[si]) + '</td><td class="sub" style="margin:0">' + esc(PROJS[m])
        + '</td><td class="num">' + num(A.psTok[m]) + '</td><td class="num">'
        + human(A.psTok[m]) + '</td></tr>';
    }).join('') || '<tr><td colspan="4">这个范围里没有记录</td></tr>';
  }

  /* 点柱子 → 下面出一条横着写的读数条（日期 / 合计 / 各来源拆分明细 / 调用次数）。
     再点同一根取消。密集模式下柱顶不标数字，这条就是看准确数的入口。 */
  function renderPick() {
    if (picked < 0 || !A || picked >= A.sel.length) {
      pickEl.hidden = true; pickEl.innerHTML = ''; return;
    }
    var k = A.sel[picked], per = A.byDaySrc[picked] || [], total = A.byDay[picked];
    var calls = 0, si, row;
    for (si = 0; si < NS; si++) {
      row = (CELL[k] || [])[si] || [];
      calls += row[1] || 0;
    }
    var h = '<span class="pk-date">' + DAYS[k] + '</span>'
          + '<span>合计 <b>' + human(total) + '</b> <span class="pk-dim">'
          + num(total) + ' tokens</span></span>';
    for (si = 0; si < NS; si++) {
      if (!per[si]) continue;
      h += '<span><i style="background:' + COLORS[si] + '"></i>' + esc(SRC[si]) + ' <b>'
         + human(per[si]) + '</b> <span class="pk-dim">'
         + (total ? (per[si] / total * 100).toFixed(1) + '%' : '-') + '</span></span>';
    }
    h += '<span class="pk-dim">' + num(calls) + ' 次调用</span>';
    pickEl.innerHTML = h;
    pickEl.hidden = false;
  }

  function setPicked(i) {
    picked = i;
    Array.prototype.forEach.call(chart.querySelectorAll('.bar-col'), function (c) {
      c.classList.toggle('picked', +c.getAttribute('data-i') === picked);
    });
    renderPick();
  }

  function syncWork() {
    var el = document.getElementById('rpWork');
    if (el) el.classList.toggle('on', onlyWork);
  }

  var A = null;
  function refresh() {
    A = compute(selectedIdx());
    renderCards(A);
    renderChart(A);
    renderSources(A);
    renderModels(A);
    renderDays(A);
    renderProjects(A);
    renderSessions(A);

    rangeBtn.classList.toggle('active', !!(FROM || TO));
    rangeText.textContent = rangeLabel();
    writeHash();
    syncWork();
    if (picked >= A.sel.length) picked = -1;   /* 范围变了，旧下标会指到别的天 */
    renderPick();
    /* 自检：**只有在"和生成时完全一致的视图"下**才能比这两个数 ——
       一旦筛了范围或开了「只看工作日」，本来就该和全量不一样，
       这时候报警就是假警报。（漏判 onlyWork 真的报过一次。） */
    var bad = (D.check && !FROM && !TO && !onlyWork &&
               (A.total !== D.check[0] || A.calls !== D.check[1]));
    warnNote.hidden = !bad;
    if (bad) warnNote.textContent = '注意：页面重算的合计（' + A.total + ' / ' + A.calls
      + '）和生成时不一致（' + D.check[0] + ' / ' + D.check[1] + '），请重新统计一次';
  }

  /* ------------------------------------------------ 日历 */

  function renderCal() {
    rpTitle.textContent = calY + ' 年 ' + (calM + 1) + ' 月';
    var lead = (new Date(Date.UTC(calY, calM, 1)).getUTCDay() + 6) % 7;
    var dim = new Date(Date.UTC(calY, calM + 1, 0)).getUTCDate();
    var html = '', i, k, cls;
    for (i = 0; i < lead; i++) html += '<button type="button" class="blank" disabled></button>';
    for (i = 1; i <= dim; i++) {
      k = dstr(calY, calM, i);
      if (k < MINDAY || k > MAXDAY) {
        html += '<button type="button" class="off" disabled>' + i + '</button>';
        continue;
      }
      cls = '';
      /* 只给"数据范围内、且不计入统计"的日子划线。
         范围外本来就没数据（.off 已经置灰），再划线会误导 ——
         比如 10-10 是调休上班日，划线就等于说它休息。 */
      var wi = DAYS.indexOf(k);
      if (onlyWork && wi >= 0 && !WORK[wi]) cls += ' rest';
      if (k === TODAY) cls += ' today';
      if (k === FROM || k === TO || k === anchor) cls += ' edge';
      else if (FROM && TO && k > FROM && k < TO) cls += ' in';
      html += '<button type="button" class="' + cls + '" data-d="' + k + '">' + i + '</button>';
    }
    rpGrid.innerHTML = html;
    rpPrev.disabled = (calY === +MINDAY.slice(0, 4) && calM === +MINDAY.slice(5, 7) - 1);
    rpNext.disabled = (calY === +MAXDAY.slice(0, 4) && calM === +MAXDAY.slice(5, 7) - 1);
    var tail = onlyWork ? ('　工作日 ' + selectedCount() + ' 天') : '';
    rpSel.textContent = (anchor
      ? ('起点 ' + anchor.slice(5) + '，再点一天选结束')
      : (FROM && TO ? FROM.slice(5) + ' ~ ' + TO.slice(5)
        : (FROM ? FROM.slice(5) + ' 起' : '全部 ' + DAYS.length + ' 天'))) + tail;
  }
  function openPop() {
    anchor = null;
    var base = FROM || MAXDAY;
    calY = +base.slice(0, 4); calM = +base.slice(5, 7) - 1;
    renderCal();
    rangePop.hidden = false;
  }
  function closePop() { rangePop.hidden = true; anchor = null; }

  rangeBtn.addEventListener('click', function (ev) {
    ev.stopPropagation();
    if (rangePop.hidden) openPop(); else closePop();
  });
  rpPrev.addEventListener('click', function () { calM--; if (calM < 0) { calM = 11; calY--; } renderCal(); });
  rpNext.addEventListener('click', function () { calM++; if (calM > 11) { calM = 0; calY++; } renderCal(); });

  rpGrid.addEventListener('click', function (ev) {
    /* ⚠️ 必须 stopPropagation：点第一下选起点时 renderCal() 会重建格子，
       被点的按钮当场变成游离节点，事件冒到 document 后会被当成"点了外面"
       → 弹层自己关掉，第二下就点不到了。 */
    ev.stopPropagation();
    var b = ev.target.closest ? ev.target.closest('button[data-d]') : null;
    if (!b || b.disabled) return;
    var d = b.getAttribute('data-d');
    if (!anchor) { anchor = d; renderCal(); return; }
    if (d < anchor) { FROM = d; TO = anchor; } else { FROM = anchor; TO = d; }
    anchor = null; refresh(); renderCal(); closePop();
  });

  rpPresets.addEventListener('click', function (ev) {
    ev.stopPropagation();
    var b = ev.target.closest ? ev.target.closest('button[data-p]') : null;
    if (!b) return;
    var p = b.getAttribute('data-p');
    if (p === 'work') {                      /* 开关，不是预设：不关弹层，还要接着挑范围 */
      onlyWork = !onlyWork;
      syncWork(); refresh(); renderCal();
      return;
    }
    anchor = null;
    if (p === 'all') { FROM = TO = null; }
    else if (p === '7' || p === '30') {
      TO = MAXDAY;
      FROM = addDays(MAXDAY, p === '7' ? -6 : -29);
      if (FROM < MINDAY) FROM = MINDAY;
    } else {
      FROM = MAXDAY.slice(0, 7) + '-01';
      if (FROM < MINDAY) FROM = MINDAY;
      TO = MAXDAY;
    }
    refresh(); renderCal(); closePop();
  });

  rpClear.addEventListener('click', function (ev) {
    ev.stopPropagation();
    FROM = TO = null; anchor = null; refresh(); renderCal(); closePop();
  });

  document.addEventListener('click', function (ev) {
    if (!rangePop.hidden && !rangeWrap.contains(ev.target)) closePop();
  });
  document.addEventListener('keydown', function (ev) { if (ev.key === 'Escape') closePop(); });

  /* ------------------------------------------------ 悬停浮层 */

  chart.addEventListener('mousemove', function (ev) {
    var col = ev.target.closest ? ev.target.closest('.bar-col') : null;
    if (!col) { tip.classList.remove('on'); return; }
    var i = +col.getAttribute('data-i');
    if (!(i >= 0) || !A) return;
    var per = A.byDaySrc[i] || [], total = A.byDay[i], h, si, has = 0;
    h = '<div class="t">' + DAYS[A.sel[i]] + '　共 ' + human(total) + '</div>';
    for (si = 0; si < NS; si++) {
      if (!per[si]) continue;
      has++;
      h += '<div class="r"><i style="background:' + COLORS[si] + '"></i>' + esc(SRC[si])
         + '<span style="margin-left:auto">' + num(per[si]) + '</span></div>';
    }
    if (!has) h += '<div class="r" style="opacity:.7">这一天没有用量</div>';
    else h += '<div class="tot">合计 ' + num(total) + ' tokens</div>';
    tip.innerHTML = h;
    tip.classList.add('on');
    var w = tip.offsetWidth, hh = tip.offsetHeight;
    var x = ev.clientX + 14, y = ev.clientY - hh - 12;
    if (x + w > innerWidth - 8) x = ev.clientX - w - 14;
    if (y < 8) y = ev.clientY + 18;
    tip.style.left = x + 'px';
    tip.style.top = y + 'px';
  });
  chart.addEventListener('mouseleave', function () { tip.classList.remove('on'); });

  chart.addEventListener('click', function (ev) {
    var col = ev.target.closest ? ev.target.closest('.bar-col') : null;
    if (!col) { setPicked(-1); return; }
    var i = +col.getAttribute('data-i');
    setPicked(picked === i ? -1 : i);
  });

  refresh();
})();
</script>

<script>
/* 「重新统计」按钮 + 静默自动刷新。
   报告是**生成那一刻的快照**，不重写就永远停在那儿 —— 用户因此以为统计坏了
   （原话："token 完全不变了"）。
   所以除了按钮，再加一条**只在用户真的在看的时候**才跑的刷新：
     · 切回本标签页 / 窗口重新获得焦点，且距上次刷新 ≥ 2 分钟 → 问一次
     · 页面一直开着 → 每 10 分钟问一次
   为什么不能高频轮询：每问一次，本地就要把全部记录重扫一遍（实测约 3.5 秒），
   轮询太密等于让机器一直在扫盘。
   数据刷新后范围丢不了：范围存在地址栏 # 里，折叠区状态存在 sessionStorage 里。
   原来那条规矩不变：**不加提示条**，只有按钮；失败时才在下方补一行说明。
   注意：file:// 打开的页面也能 fetch 本地服务（服务端带了 CORS *）。 */
(function () {
  var btn = document.getElementById('regenBtn');
  var note = document.getElementById('regenNote');
  if (!btn) return;
  var API = 'http://127.0.0.1:8791';
  var label = btn.textContent;
  /* 本页数据是 BuiltAt 那一刻算出来的；服务端回报的 generatedAt 比它新 = 有新数据 */
  var BUILT_AT = +btn.getAttribute('data-gen') || 0;
  var busy = false, lastAsk = 0;

  /* 自动刷新会把页面重新载入，折叠区（会话明细等）的展开状态不能跟着丢 */
  var FOLD_KEY = 'wbUsageFolds';
  var folds = document.querySelectorAll('details.fold');
  (function restoreFolds() {
    var saved;
    try { saved = JSON.parse(sessionStorage.getItem(FOLD_KEY) || '[]'); } catch (e) { return; }
    Array.prototype.forEach.call(folds, function (el, i) { if (saved[i]) el.open = true; });
  })();
  Array.prototype.forEach.call(folds, function (el, i) {
    el.addEventListener('toggle', function () {
      var cur = [];
      Array.prototype.forEach.call(folds, function (x) { cur.push(!!x.open); });
      try { sessionStorage.setItem(FOLD_KEY, JSON.stringify(cur)); } catch (e) {}
    });
  });

  /* silent=true 时不碰按钮外观 —— 自动刷新该像面板自己在呼吸，
     而不是像有人替你按了一下按钮 */
  function ask(silent) {
    if (busy) return;
    busy = true;
    lastAsk = Date.now();
    if (!silent) {
      btn.disabled = true;
      btn.textContent = '统计中…';
      if (note) note.hidden = true;
    }
    fetch(API + '/regenerate', { cache: 'no-store' })
      .then(function (r) { if (!r.ok) throw new Error('bad status'); return r.json(); })
      .then(function (d) {
        if (!d || d.ok === false) throw new Error((d && d.error) || 'failed');
        busy = false;
        /* 服务端每次都重写文件、generatedAt 必然更新；所以判断"有没有新数据"
           只能靠它比本页的生成时刻新 —— 新了就重载，没新就什么都不做。 */
        if (d.generatedAt && d.generatedAt > BUILT_AT) location.reload();
      })
      .catch(function () {
        busy = false;
        if (silent) return;                    /* 自动那条路失败就安静退场，不打扰 */
        btn.disabled = false;
        btn.textContent = label;
        if (note) {
          note.hidden = false;
          note.textContent = '连不上本地状态服务 —— 双击项目里的「查看用量.command」'
                           + '重新统计，然后刷新本页';
        }
      });
  }

  btn.addEventListener('click', function () { ask(false); });

  var MIN_GAP = 120000;                        /* 两次刷新之间至少隔 2 分钟 */
  function auto() {
    if (document.hidden || busy) return;
    if (Date.now() - lastAsk < MIN_GAP) return;
    if (Date.now() - BUILT_AT < 30000) return; /* 刚重载完，别立刻再扫一遍 */
    ask(true);
  }
  setInterval(auto, 600000);                   /* 一直开着：每 10 分钟 */
  document.addEventListener('visibilitychange', auto);
  window.addEventListener('focus', auto);
})();
</script>
</body>
</html>
"""
    repl = {
        "__TITLE__": html.escape(title), "__GEN__": generated,
        "__GENEPOCH__": str(generated_epoch),
        "__H1__": "Token 面板",
        "__CHART_JSON__": report_json,
        "__NDAYS__": str(len(report["days"])),
    }
    for k, v in repl.items():
        html_out = html_out.replace(k, v)
    return html_out


# ---------------------------------------------------------------- 入口

def resolve_range(args):
    """统计范围。默认**全部历史** —— 两个来源的活跃期不一样
    （ChatGPT 集中在 9 月，DeepSeek 集中在 10 月），
    按月切会各缺一半，看不到"一共用了多少"。
    """
    now = dt.datetime.now()
    month = getattr(args, "month", None)
    if month:
        try:
            y, m = month.split("-")
            start = dt.datetime(int(y), int(m), 1)
        except Exception:
            print("月份格式不对，应该像 2026-09")
            sys.exit(1)
        end = dt.datetime(start.year + (start.month == 12), (start.month % 12) + 1, 1)
        return "%s 年 %s 月" % (y, int(m)), lambda w: start <= w < end
    if getattr(args, "days", None):
        start = (now - dt.timedelta(days=args.days - 1)).replace(
            hour=0, minute=0, second=0, microsecond=0)
        return "最近 %d 天" % args.days, lambda w: w >= start
    since = getattr(args, "since", None)
    if since:
        try:
            start = dt.datetime.strptime(since, "%Y-%m-%d")
        except ValueError:
            print("日期格式不对，应该像 2026-09-01")
            sys.exit(1)
        return "%s 起" % since, lambda w: w >= start
    return "全部历史", lambda w: True


def generate(all_models=False, all_=False, days=None, out=None, write_html=True,
             sources=None, month=None, since=None):
    """扫描本地记录 → 聚合 → 写出报告 HTML。返回 (agg, title, path, meta)。

    为什么抽成函数：**报告是"生成那一刻"的快照，不重写就永远停在那儿。**
    真实踩过 —— 用户上午 10:50 生成过一次，之后一直在看那个文件，
    以为统计坏了（"今天这么大的量怎么不算了"）。
    所以状态服务要能在用户点「重新统计」时就地重算一遍。

    all_models / all_ 保留是为了不破坏老调用方（pet_daemon 用无参调用）；
    现在两个来源默认都算，不再有"只看 DeepSeek"的过滤。
    """
    if not os.path.isdir(PROJECTS_DIR) and not os.path.isdir(CODEX_SESSIONS) and not os.path.isdir(CLAUDE_PROJECTS):
        raise RuntimeError("既找不到 %s，也找不到 %s" % (PROJECTS_DIR, CODEX_SESSIONS))

    class _Args:
        pass
    a = _Args()
    a.all, a.days, a.month, a.since = all_, days, month, since

    title, within = resolve_range(a)
    records = [r for r in iter_all_calls() if within(r["when"])]
    if sources:
        keep = set(sources)
        records = [r for r in records if r["source"] in keep]
        title += "　·　" + " + ".join(sorted(keep))
    agg = aggregate(records)

    meta = load_session_meta()
    for r in records:                      # ChatGPT 的标题/路径不在本地库里，从记录里补
        sid = r["session"]
        if sid.startswith(("codex:", "claude:")) and sid not in meta:
            meta[sid] = {"title": r.get("title") or "", "cwd": r.get("project") or ""}

    path = out or OUT_HTML
    if write_html:
        html_out = build_html(agg, title, meta, records)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(html_out)
        os.replace(tmp, path)          # 原子替换：别让"正在写的半成品"被浏览器读到
    return agg, title, path, meta


def main():
    ap = argparse.ArgumentParser(description="DeepSeek + ChatGPT token 用量统计")
    ap.add_argument("--month", help="指定月份，如 2026-09")
    ap.add_argument("--all", action="store_true", help="全部历史（现在是默认行为）")
    ap.add_argument("--days", type=int, help="最近 N 天")
    ap.add_argument("--since", help="从某天开始，如 2026-09-01")
    ap.add_argument("--source", action="append", choices=["codex", "deepseek", "claude"],
                    help="只看某个来源，可重复给；不给就是全部")
    ap.add_argument("--all-models", action="store_true",
                    help="（保留兼容）现在默认就统计全部模型")
    ap.add_argument("--no-html", action="store_true", help="不生成 HTML 报告")
    ap.add_argument("--open", action="store_true", help="生成后自动打开报告")
    args = ap.parse_args()

    if not os.path.isdir(PROJECTS_DIR) and not os.path.isdir(CODEX_SESSIONS) and not os.path.isdir(CLAUDE_PROJECTS):
        print("既找不到 %s，也找不到 %s" % (PROJECTS_DIR, CODEX_SESSIONS))
        sys.exit(1)

    sources = None
    if args.source:
        sources = [{"codex": SRC_CODEX, "deepseek": SRC_DEEPSEEK, "claude": SRC_CLAUDE}[s] for s in args.source]

    # 走同一个 generate() —— 命令行和网页上那个「重新统计」按钮
    # 必须是同一条代码路径，否则两边数字会悄悄不一致
    agg, title, path, meta = generate(all_=args.all, days=args.days,
                                      month=args.month, since=args.since,
                                      sources=sources,
                                      write_html=not args.no_html)
    print_terminal(agg, title, meta)

    if not args.no_html:
        print("  报告已生成：%s" % path)
        if args.open:
            rc = os.system('open "%s"' % path)
            if rc != 0:
                print("  自动打开失败，请手动双击：%s" % path)


if __name__ == "__main__":
    main()
