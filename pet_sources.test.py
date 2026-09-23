#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
状态源适配器的回归测试。

    python3 pet_sources.test.py

用**合成记录 + 受控时间戳**测，不依赖真实数据 ——
否则只有在"你恰好正在用那个工具"的瞬间才能测到活跃状态。
"""
import datetime as dt
import json
import os
import shutil
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pet_sources as SRC  # noqa: E402


def iso(ago_sec):
    """生成 ago_sec 秒前的时间戳，格式同 Codex（UTC + Z）"""
    t = dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=ago_sec)
    return t.strftime("%Y-%m-%dT%H:%M:%S.") + "%03dZ" % (t.microsecond // 1000)


# ---------------- WorkBuddy 源（主力源，之前完全没有覆盖） ----------------
#
# 之所以必须补：`sleeping` 曾经"写了但永远走不到"，而它恰好发生在这个源上。
# 造一棵假的 ~/.workbuddy 目录树（projects/*.jsonl + sessions/*.json 心跳），
# 就能在受控时间戳下测真实解析逻辑，不依赖"你此刻正好在用 WorkBuddy"。

def build_workbuddy(root, events, heartbeat_age=5, session_id="sid-test"):
    projects = os.path.join(root, "projects", "demo-project")
    sessions = os.path.join(root, "sessions")
    os.makedirs(projects, exist_ok=True)
    os.makedirs(sessions, exist_ok=True)
    now = int(time.time() * 1000)
    p = os.path.join(projects, session_id + ".jsonl")
    with open(p, "w", encoding="utf-8") as fh:
        for ago_sec, t, extra in events:
            rec = {"timestamp": now - int(ago_sec * 1000), "type": t}
            rec.update(extra or {})
            fh.write(json.dumps(rec) + "\n")
    with open(os.path.join(sessions, session_id + ".json"), "w", encoding="utf-8") as fh:
        json.dump({"sessionId": session_id, "cwd": "/tmp/demo-project",
                   "lastHeartbeat": now - int(heartbeat_age * 1000)}, fh)
    return p


WB_CASES = [
    ("正在调工具",      5, "function_call", {}, "working"),
    ("模型在推理",      3, "reasoning", {}, "thinking"),
    ("任务完成",        5, "message", {"role": "assistant", "content": "done"}, "success"),
    ("助手回完一段",   40, "message", {"role": "assistant", "content": "done"}, "waiting"),
    ("助手回完 50 秒", 50, "message", {"role": "assistant", "content": "done"}, "idle"),
    ("助手回完 61 秒", 61, "message", {"role": "assistant", "content": "done"}, "sleeping"),
    # ⚠️ 这条是关键：它超过 180 秒的扫描窗口，会走"提前 break"的分支。
    #    如果没把"最后一次活动时刻"单独留下，la 会是 None → 永远不睡。
    ("助手回完很久",  300, "message", {"role": "assistant", "content": "done"}, "sleeping"),
    ("用户刚发言",      3, "message", {"role": "user", "content": "帮我写个脚本"}, "thinking"),
]


def workbuddy_tests():
    print("\nWorkBuddy 状态推断（主力源）：")
    ok = 0
    save_p, save_s = SRC.PROJECTS_DIR, SRC.SESSIONS_DIR
    try:
        for name, ago, t, extra, want in WB_CASES:
            tmp = tempfile.mkdtemp()
            try:
                build_workbuddy(tmp, [(ago, t, extra)])
                SRC.PROJECTS_DIR = os.path.join(tmp, "projects")
                SRC.SESSIONS_DIR = os.path.join(tmp, "sessions")
                got = SRC.WorkBuddySource().snapshot()
                good = got["state"] == want
                ok += good
                extra_note = ""
                if want == "sleeping" and got["state"] != "sleeping":
                    extra_note = f"  ← lastActivity={got.get('lastActivity')}（取不到就永远不睡）"
                print(f"  {'✓' if good else '✗'} {name:14} 期望 {want:9} 实得 {got['state']}{extra_note}")
            finally:
                shutil.rmtree(tmp, ignore_errors=True)
    finally:
        SRC.PROJECTS_DIR, SRC.SESSIONS_DIR = save_p, save_s

    # 心跳过期 = 没在跑 → offline（而不是 sleeping：offline 没有可信时刻，不猜）
    tmp = tempfile.mkdtemp()
    try:
        build_workbuddy(tmp, [(5, "reasoning", {})], heartbeat_age=600)
        SRC.PROJECTS_DIR = os.path.join(tmp, "projects")
        SRC.SESSIONS_DIR = os.path.join(tmp, "sessions")
        got = SRC.WorkBuddySource().snapshot()
        good = got["state"] == "offline"
        ok += good
        print(f"  {'✓' if good else '✗'} {'心跳过期':14} 期望 offline  实得 {got['state']}")
    finally:
        SRC.PROJECTS_DIR, SRC.SESSIONS_DIR = save_p, save_s
        shutil.rmtree(tmp, ignore_errors=True)

    return ok, len(WB_CASES) + 1


def build_codex(root, events):
    d = os.path.join(root, "2026", "09", "23")
    os.makedirs(d, exist_ok=True)
    p = os.path.join(d, "rollout-2026-09-23T00-00-00-test.jsonl")
    with open(p, "w", encoding="utf-8") as fh:
        fh.write(json.dumps({"timestamp": iso(600), "type": "session_meta",
                             "payload": {"cwd": "/tmp/demo-project"}}) + "\n")
        for ago, t, pl in events:
            fh.write(json.dumps({"timestamp": iso(ago), "type": t, "payload": pl}) + "\n")
    return p


MSG_U = ("response_item", {"type": "message", "role": "user",
                           "content": [{"type": "input_text", "text": "帮我写个脚本"}]})
MSG_A = ("response_item", {"type": "message", "role": "assistant",
                           "content": [{"type": "output_text", "text": "done"}]})

CASES = [
    ("正在调工具",     5, ("response_item", {"type": "custom_tool_call", "name": "shell"}), "working"),
    ("模型在推理",     3, ("response_item", {"type": "reasoning"}), "thinking"),
    ("工具刚返回",     6, ("response_item", {"type": "custom_tool_call_output", "output": "x"}), "thinking"),
    ("任务完成",       5, ("event_msg", {"type": "task_complete"}), "success"),
    ("助手刚回复",     8, MSG_A, "success"),
    ("助手回完一段",  40, MSG_A, "waiting"),
    ("助手回完 50 秒", 50, MSG_A, "idle"),
    ("助手回完 61 秒", 61, MSG_A, "sleeping"),
    ("助手回完很久", 300, MSG_A, "sleeping"),
    ("用户刚发言",     3, MSG_U, "thinking"),
    ("用户发言 20 秒", 20, MSG_U, "thinking"),
    ("被中断",         5, ("event_msg", {"type": "turn_aborted"}), "error"),
    ("早就停了",     300, ("response_item", {"type": "reasoning"}), "sleeping"),
]

# 睡眠规则的直接单测 —— 把「多久算睡着」「哪些状态才睡」钉死。
# 这条规则是"推算"出来的（不是从记录里读的），最容易在改动时被绕过，
# 所以除了走适配器，还要单独测一遍分支。
SLEEP_CASES = [
    ("空闲 59 秒",  "idle",     59_000, "idle"),
    ("空闲 60 秒",  "idle",     60_000, "sleeping"),
    ("等你回复够久", "waiting",  60_000, "sleeping"),
    ("刚完成够久",   "success",  60_000, "sleeping"),
    ("执行中再久也不睡", "working", 600_000, "working"),
    ("思考中再久也不睡", "thinking", 600_000, "thinking"),
    ("离线不猜",    "offline",  600_000, "offline"),
    ("没有活动时刻不睡", "idle",      None,  "idle"),
]


def sleep_rule_tests():
    print("\n睡眠规则：")
    ok = 0
    for name, state, ago_ms, want in SLEEP_CASES:
        now = 10 ** 13
        la = None if ago_ms is None else now - ago_ms
        got = SRC._sleep_if_quiet(state, la, now)
        good = got == want
        ok += good
        print(f"  {'✓' if good else '✗'} {name:18} 期望 {want:9} 实得 {got}")
    return ok, len(SLEEP_CASES)


def main():
    ok = 0
    print("Codex 状态推断：")
    for name, ago, (t, pl), want in CASES:
        tmp = tempfile.mkdtemp()
        try:
            build_codex(tmp, [(ago, t, pl)])
            got = SRC.CodexSource(root=tmp).snapshot()
            good = got["state"] == want
            ok += good
            print(f"  {'✓' if good else '✗'} {name:14} 期望 {want:9} 实得 {got['state']}")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    # 没有数据时的行为
    tmp = tempfile.mkdtemp()
    try:
        s = SRC.CodexSource(root=tmp)
        assert s.snapshot()["state"] == "idle", "空目录应返回 idle"
        ok += 1
        print("  ✓ 没有会话文件时返回 idle")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    n = SRC.NullSource().snapshot()
    assert n["state"] == "idle" and n["source"] == "none"
    ok += 1
    print("  ✓ NullSource 恒为 idle")

    sok, stotal = sleep_rule_tests()
    ok += sok

    wok, wtotal = workbuddy_tests()
    ok += wok

    total = len(CASES) + 2 + stotal + wtotal
    print(f"\n  {ok}/{total} 通过")
    return 0 if ok == total else 1


if __name__ == "__main__":
    sys.exit(main())
