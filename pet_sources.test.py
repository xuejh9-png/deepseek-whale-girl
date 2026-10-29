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

def build_workbuddy(root, events, heartbeat_age=5, session_id="sid-test", log_age=None):
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
    # 文件刚写完，mtime 必然是"现在"。要造"日志很久没写"的场景必须显式改 mtime，
    # 否则测的永远是"刚写过"这一种情况。
    if log_age is not None:
        stamp = time.time() - log_age
        os.utime(p, (stamp, stamp))
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

    # 两个证据都过期 = 没在跑 → offline
    # （而不是 sleeping：offline 没有可信时刻，不猜）
    tmp = tempfile.mkdtemp()
    try:
        build_workbuddy(tmp, [(5, "reasoning", {})], heartbeat_age=600, log_age=600)
        SRC.PROJECTS_DIR = os.path.join(tmp, "projects")
        SRC.SESSIONS_DIR = os.path.join(tmp, "sessions")
        got = SRC.WorkBuddySource().snapshot()
        good = got["state"] == "offline"
        ok += good
        print(f"  {'✓' if good else '✗'} {'心跳+日志都过期':14} 期望 offline  实得 {got['state']}")
    finally:
        SRC.PROJECTS_DIR, SRC.SESSIONS_DIR = save_p, save_s
        shutil.rmtree(tmp, ignore_errors=True)

    # 回归：2026-10-26 用户报"我在跑任务，宠物却一直空闲"。
    # 实测 WorkBuddy 的心跳**只在会话启动时写一次**，一分钟后必然"过期"；
    # 只看心跳会把正在干活的会话踢出列表。日志刚写过 = 真的在干活。
    tmp = tempfile.mkdtemp()
    try:
        build_workbuddy(tmp, [(3, "reasoning", {})], heartbeat_age=600, log_age=0)
        SRC.PROJECTS_DIR = os.path.join(tmp, "projects")
        SRC.SESSIONS_DIR = os.path.join(tmp, "sessions")
        got = SRC.WorkBuddySource().snapshot()
        good = got["state"] == "thinking"
        ok += good
        print(f"  {'✓' if good else '✗'} {'心跳停了但日志在写':14} 期望 thinking 实得 {got['state']}")
    finally:
        SRC.PROJECTS_DIR, SRC.SESSIONS_DIR = save_p, save_s
        shutil.rmtree(tmp, ignore_errors=True)

    # 回归（更需要盯住的那一半）：僵尸会话的心跳比谁都新、日志却七天没写。
    # 它不能压过真正在跑的那个会话 —— AutoSource 必须选到正在干活的。
    tmp = tempfile.mkdtemp()
    try:
        build_workbuddy(tmp, [(3, "function_call", {})], heartbeat_age=600,
                        session_id="zzz-live", log_age=0)
        build_workbuddy(tmp, [(604800, "message", {"role": "assistant", "content": "done"})],
                        heartbeat_age=1, session_id="aaa-zombie", log_age=604800)
        SRC.PROJECTS_DIR = os.path.join(tmp, "projects")
        SRC.SESSIONS_DIR = os.path.join(tmp, "sessions")
        save_c = SRC.CODEX_SESSIONS
        SRC.CODEX_SESSIONS = os.path.join(tmp, "no-codex")
        try:
            got = SRC.AutoSource().snapshot()
        finally:
            SRC.CODEX_SESSIONS = save_c
        good = got["state"] == "working" and got.get("sessionId") == "zzz-live"
        ok += good
        print(f"  {'✓' if good else '✗'} {'僵尸心跳不压活跃会话':14} 期望 working/zzz-live 实得 "
              f"{got['state']}/{got.get('sessionId')}")
    finally:
        SRC.PROJECTS_DIR, SRC.SESSIONS_DIR = save_p, save_s
        shutil.rmtree(tmp, ignore_errors=True)

    return ok, len(WB_CASES) + 3


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


def claude_tests():
    from unittest.mock import patch
    from pathlib import Path
    cases = json.loads((Path(__file__).parent / "tests/claude-state-cases.json").read_text(encoding="utf-8"))
    passed = 0
    now = int(time.time() * 1000)
    print("\nClaude 跨平台共用场景：")
    for case in cases:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            project.mkdir()
            events = []
            for event in case["events"]:
                row = dict(event)
                ago = row.pop("ago")
                row.update(timestamp=dt.datetime.fromtimestamp((now - ago * 1000) / 1000, dt.timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
                           cwd="/demo", sessionId="claude-demo")
                events.append(row)
            log = project / "session.jsonl"
            log.write_text("\n".join(json.dumps(r) for r in events) + "\n", encoding="utf-8")
            source = SRC.ClaudeSource(root=tmp)
            with patch.object(SRC, "now_ms", return_value=now):
                got = source.snapshot()
                good = got["state"] == case["state"]
                passed += good
                print(f"  {'✓' if good else '✗'} {case['name']}: {got['state']} (期望 {case['state']})")
                assert source.snapshot() == got, "缓存不能改变结果"
    with tempfile.TemporaryDirectory() as tmp:
        p = Path(tmp) / "project"
        p.mkdir()
        row = {"timestamp": iso(1), "type": "assistant", "sessionId": "claude-live",
               "message": {"content": [{"type": "tool_use"}]}}
        log = p / "real.jsonl"
        log.write_text(json.dumps(row) + "\n", encoding="utf-8")
        for i in range(5):
            (p / f"meta{i}.jsonl").write_text(json.dumps({"type": "attachment", "timestamp": iso(0)}) + "\n", encoding="utf-8")
        sub = p / "subagents"
        sub.mkdir()
        (sub / "agent.jsonl").write_text(json.dumps({"type": "assistant", "timestamp": iso(0), "message": {"content": [{"type": "text"}]}}) + "\n", encoding="utf-8")
        source = SRC.ClaudeSource(root=tmp)
        assert source.snapshot()["state"] == "working", "元数据/子代理不能遮住主会话"
        log.write_text(json.dumps(dict(row, type="user", message={"content": "[Request interrupted by user]"})) + "\n", encoding="utf-8")
        assert source.snapshot()["state"] == "error", "日志变更后缓存必须失效"
        log.unlink()
        assert source.snapshot()["state"] == "offline", "删掉会话不能残留工作状态"
        with patch.dict(os.environ, {"PET_CLAUDE_DIR": tmp}):
            assert SRC.pick("claude").root == tmp
        auto = SRC.AutoSource()
        auto._sources = [source]
        assert auto.snapshot()["source"] == "claude"
        assert "claude" in SRC.REGISTRY
        passed += 6
    return passed, len(cases) + 6


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

    cok, ctotal = claude_tests()
    ok += cok
    total = len(CASES) + 2 + stotal + wtotal + ctotal
    print(f"\n  {ok}/{total} 通过")
    return 0 if ok == total else 1


if __name__ == "__main__":
    sys.exit(main())
