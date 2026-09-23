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

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pet_sources as SRC  # noqa: E402


def iso(ago_sec):
    """生成 ago_sec 秒前的时间戳，格式同 Codex（UTC + Z）"""
    t = dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=ago_sec)
    return t.strftime("%Y-%m-%dT%H:%M:%S.") + "%03dZ" % (t.microsecond // 1000)


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
    ("助手回完很久", 300, MSG_A, "idle"),
    ("用户刚发言",     3, MSG_U, "thinking"),
    ("用户发言 20 秒", 20, MSG_U, "thinking"),
    ("被中断",         5, ("event_msg", {"type": "turn_aborted"}), "error"),
    ("早就停了",     300, ("response_item", {"type": "reasoning"}), "idle"),
]


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

    total = len(CASES) + 2
    print(f"\n  {ok}/{total} 通过")
    return 0 if ok == total else 1


if __name__ == "__main__":
    sys.exit(main())
