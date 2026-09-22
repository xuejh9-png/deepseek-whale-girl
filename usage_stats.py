#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
WorkBuddy Token 用量统计（纯 Python 标准库，零依赖）

数据来源：~/.workbuddy/projects/<项目>/<会话ID>.jsonl
每条 LLM 调用都带 providerData.rawUsage（prompt / completion / total tokens），
这是逐次调用的完整记账，也是本地唯一可信的 token 来源
（traces/ 目录里的 totalTokens 覆盖率仅 13%，不能用）。

口径：
  累计 Token  = sum(total_tokens)              —— 接口账单口径
  真正新增    = sum(prompt - cached) + sum(completion)  —— 剔掉重复重读的部分

用法：
    python3 usage_stats.py                # 看本月
    python3 usage_stats.py --all          # 全部历史
    python3 usage_stats.py --month 2026-09
    python3 usage_stats.py --days 7       # 最近 7 天
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
import sqlite3
import sys

WB_ROOT = os.path.expanduser("~/.workbuddy")
PROJECTS_DIR = os.path.join(WB_ROOT, "projects")
DB_PATH = os.path.join(WB_ROOT, "workbuddy.db")
OUT_HTML = os.path.join(os.path.dirname(os.path.abspath(__file__)), "用量报告.html")


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
    if "deepseek" not in s:
        return name or "未知模型"
    for a, b, label in MODEL_ALIAS:
        if a in s and (not b or b in s):
            return label
    return name


def is_deepseek(name):
    return "deepseek" in (name or "").lower()

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


def iter_calls():
    """逐条产出每次 LLM 调用的用量记录。"""
    seen = set()
    pattern = os.path.join(PROJECTS_DIR, "*", "*.jsonl")
    for path in sorted(glob.glob(pattern)):
        project = os.path.basename(os.path.dirname(path))
        session = os.path.basename(path)[:-6]
        try:
            fh = open(path, "r", errors="ignore")
        except OSError:
            continue
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
                # 防重放：同毫秒 + 同用量 + 同模型 视为同一次
                key = (ts, prompt, completion, model)
                if key in seen:
                    continue
                seen.add(key)
                details = ru.get("prompt_tokens_details") or {}
                yield {
                    "ts": ts,
                    "when": dt.datetime.fromtimestamp(ts / 1000.0),
                    "prompt": prompt,
                    "completion": completion,
                    "total": total,
                    "cached": details.get("cached_tokens", 0) or 0,
                    "model": model,
                    "project": project,
                    "session": session,
                }


# ---------------------------------------------------------------- 聚合

def aggregate(records):
    agg = {
        "total": 0, "prompt": 0, "completion": 0, "cached": 0, "calls": 0,
        "by_day": collections.Counter(),
        "by_model": collections.Counter(),
        "by_project": collections.Counter(),
        "by_session": collections.Counter(),
        "model_calls": collections.Counter(),
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
        agg["by_day"][d] += r["total"]
        agg["by_model"][r["model"]] += r["total"]
        agg["model_calls"][r["model"]] += 1
        agg["by_project"][r["project"]] += r["total"]
        agg["by_session"][r["session"]] += r["total"]
        s = span.setdefault(r["session"], [r["ts"], r["ts"]])
        if r["ts"] < s[0]:
            s[0] = r["ts"]
        if r["ts"] > s[1]:
            s[1] = r["ts"]
        t = r["when"]
        first = t if first is None else min(first, t)
        last = t if last is None else max(last, t)

    agg["first"], agg["last"] = first, last
    # 单次会话跨度的最大值（首尾时差）
    if span:
        _, (a, b) = max(span.items(), key=lambda kv: kv[1][1] - kv[1][0])
        agg["longest_sec"] = (b - a) / 1000.0
        agg["longest_from"] = dt.datetime.fromtimestamp(a / 1000).strftime("%m-%d %H:%M")
        agg["longest_to"] = dt.datetime.fromtimestamp(b / 1000).strftime("%m-%d %H:%M")
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
    print("  WorkBuddy Token 用量  ·  %s" % title)
    print("=" * w)
    if agg["first"]:
        print("  统计范围：%s ~ %s" % (
            agg["first"].strftime("%Y-%m-%d %H:%M"), agg["last"].strftime("%Y-%m-%d %H:%M")))
    print()
    print("  累计 Token        %16s" % human(agg["total"]))
    print("  峰值 Token        %16s   （%s）" % (human(agg["peak_val"]), agg["peak_day"]))
    print("  最长聊天时长      %16s" % fmt_dur(agg["longest_sec"]))
    print("  连续天数          %16s   （当前 %d / 最长 %d，共活跃 %d 天）"
          % ("%d 天" % agg["streak_cur"], agg["streak_cur"], agg["streak_best"], agg["active_days"]))
    print("-" * w)
    print("  调用 %s 次 ｜ 输入 %s ｜ 输出 %s"
          % (comma(agg["calls"]), human(agg["prompt"]), human(agg["completion"])))
    print("=" * w)

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

def heatmap(by_day):
    """GitHub 风格的活动格子，按周一为每周第一行对齐。"""
    if not by_day:
        return "", ""
    d0 = dt.datetime.strptime(min(by_day), "%Y-%m-%d").date()
    d1 = dt.datetime.strptime(max(by_day), "%Y-%m-%d").date()
    peak = max(by_day.values()) or 1

    cells, month_labels = [], []
    cur = d0 - dt.timedelta(days=d0.weekday())   # 回退到本周周一
    last_month = None
    col_index = 0
    while cur <= d1:
        if cur.weekday() == 0:                    # 新的一列
            if last_month != cur.month:
                month_labels.append((col_index, "%d月" % cur.month))
                last_month = cur.month
            col_index += 1
        key = cur.strftime("%Y-%m-%d")
        if cur < d0 or cur > d1:
            cells.append('<div class="hc blank"></div>')
        else:
            v = by_day.get(key, 0)
            lvl = min(4, max(1, int((v / peak) ** 0.5 * 4 + 0.999))) if v else 0
            cells.append('<div class="hc l%d" title="%s：%s tokens"></div>' % (lvl, key, comma(v)))
        cur += dt.timedelta(days=1)

    labels = "".join('<span style="left:%dpx">%s</span>' % (c * 16, m)
                     for c, m in month_labels)
    return "".join(cells), labels


def build_html(agg, title, meta):
    first = agg["first"].strftime("%Y-%m-%d %H:%M") if agg["first"] else "-"
    last = agg["last"].strftime("%Y-%m-%d %H:%M") if agg["last"] else "-"
    generated = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # 看板娘台词：全部来自真实数据
    mascot_lines = [
        "本月累计 %s token 啦～" % human(agg["total"]),
        "最多的一天是 %s，%s！" % (agg["peak_day"][5:], human(agg["peak_val"])),
        "我们连续 %d 天都见面了～" % agg["streak_cur"],
        "%s 次调用，辛苦你啦" % comma(agg["calls"]),
        "有一次我们聊了 %s 呢" % fmt_dur(agg["longest_sec"]),
        "这个月有 %d 天你来找过我" % agg["active_days"],
        "最长连着 %d 天，好厉害！" % agg["streak_best"],
    ]

    # 按天柱状
    days = sorted(agg["by_day"])
    peak = max(agg["by_day"].values()) if days else 1
    bars = "".join(
        '<div class="bar-col" title="%s：%s tokens">'
        '<div class="bar-val">%s</div>'
        '<div class="bar" style="height:%dpx"></div>'
        '<div class="bar-day">%s</div></div>'
        % (d, comma(agg["by_day"][d]), human(agg["by_day"][d]),
           max(2, int(agg["by_day"][d] / peak * 150)), d[5:])
        for d in days)

    day_rows = "".join("<tr><td>%s</td><td class='num'>%s</td><td class='num'>%s</td></tr>"
                       % (d, comma(agg["by_day"][d]), human(agg["by_day"][d])) for d in days)

    model_rows = "".join(
        "<tr><td>%s</td><td class='num'>%s</td><td class='num'>%s</td><td class='num'>%s</td></tr>"
        % (html.escape(m), comma(agg["model_calls"][m]), comma(v), human(v))
        for m, v in agg["by_model"].most_common())

    sess_rows = "".join(
        "<tr><td class='rk'>%d</td><td>%s<div class='sub'>%s</div></td>"
        "<td class='num'>%s</td><td class='num'>%s</td></tr>"
        % (i, html.escape((meta.get(sid) or {}).get("title") or ("会话 " + sid[:8])),
           html.escape(((meta.get(sid) or {}).get("cwd") or "").replace(os.path.expanduser("~"), "~")),
           comma(v), human(v))
        for i, (sid, v) in enumerate(agg["by_session"].most_common(200), 1))

    proj_rows = "".join("<tr><td>%s</td><td class='num'>%s</td><td class='num'>%s</td></tr>"
                        % (html.escape(p), comma(v), human(v))
                        for p, v in agg["by_project"].most_common())

    hm_cells, hm_labels = heatmap(agg["by_day"])
    # 热力图要攒够周数才好看，少于 8 周先不占版面
    heat_section = ""
    if agg["by_day"]:
        d0 = dt.datetime.strptime(min(agg["by_day"]), "%Y-%m-%d").date()
        d1 = dt.datetime.strptime(max(agg["by_day"]), "%Y-%m-%d").date()
        weeks = (d1 - (d0 - dt.timedelta(days=d0.weekday()))).days // 7 + 1
        if weeks >= 8:
            heat_section = """  <section>
    <h2>Token 活动</h2>
    <div class="heat">%s</div>
    <div class="heat-months">%s</div>
    <div class="legend">少 <div class="hc l0"></div><div class="hc l1"></div>
      <div class="hc l2"></div><div class="hc l3"></div><div class="hc l4"></div> 多</div>
  </section>

""" % (hm_cells, hm_labels)

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
  .meta{color:var(--muted);font-size:13px}
  .cards{display:grid;grid-template-columns:repeat(3,1fr);gap:12px;margin:20px 0}
  @media (max-width:720px){.cards{grid-template-columns:1fr 1fr}}
  .card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px 16px}
  .card .k{font-size:12px;color:var(--muted);margin-bottom:4px}
  .card .v{font-size:24px;font-weight:600;letter-spacing:-.02em}
  .card .s{font-size:11px;color:var(--muted);margin-top:2px}
  .card.hi{border-color:#cfe0ff;background:linear-gradient(180deg,#f7faff,#fff)}
  .card.hi .v{color:var(--brand)}
  section,.fold{background:var(--card);border:1px solid var(--line);border-radius:12px;
    padding:20px;margin-bottom:16px}
  h2{font-size:15px;margin:0 0 16px;font-weight:600}
  h3.mini{font-size:12px;margin:0 0 12px;font-weight:600;color:var(--muted)}
  .heat{display:grid;grid-auto-flow:column;grid-template-rows:repeat(7,13px);
    gap:3px;overflow-x:auto;padding:4px 0 2px}
  .hc{width:13px;height:13px;border-radius:3px;background:#eef1f5}
  .hc.blank{background:transparent}
  .hc.l1{background:#cddffd}.hc.l2{background:#9dbffb}
  .hc.l3{background:#5f92f5}.hc.l4{background:#2b6cff}
  .heat-months{position:relative;height:16px;margin-top:2px}
  .heat-months span{position:absolute;top:0;font-size:10px;color:var(--muted);white-space:nowrap}
  .legend{display:flex;align-items:center;gap:5px;color:var(--muted);font-size:11px;
    justify-content:flex-end;margin-top:8px}
  .legend .hc{width:11px;height:11px}
  .chart{display:flex;align-items:flex-end;gap:6px;height:200px;overflow-x:auto;padding-top:8px}
  .bar-col{flex:1 0 42px;display:flex;flex-direction:column;align-items:center;
    justify-content:flex-end;height:100%}
  .bar-val{font-size:10px;color:var(--muted);margin-bottom:4px;white-space:nowrap}
  .bar{width:100%;max-width:34px;background:linear-gradient(180deg,#5b93ff,#2b6cff);
    border-radius:5px 5px 0 0;min-height:2px}
  .bar-day{font-size:10px;color:var(--muted);margin-top:6px;white-space:nowrap}
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

  /* ---- 看板娘 ---- */
  .mascot{position:fixed;right:20px;bottom:20px;z-index:50;cursor:pointer;
    display:flex;flex-direction:column;align-items:center;-webkit-user-select:none;user-select:none}
  .mascot .bubble{position:relative;max-width:210px;background:#fff;border:1px solid var(--line);
    border-radius:12px;padding:9px 13px;font-size:12px;line-height:1.55;color:var(--ink);
    box-shadow:0 8px 22px rgba(20,30,60,.12);margin-bottom:10px;text-align:center;
    opacity:0;transform:translateY(6px);transition:opacity .22s,transform .22s;pointer-events:none}
  .mascot .bubble.show{opacity:1;transform:translateY(0)}
  .mascot .bubble::after{content:"";position:absolute;left:50%;bottom:-6px;width:10px;height:10px;
    background:#fff;border-right:1px solid var(--line);border-bottom:1px solid var(--line);
    transform:translateX(-50%) rotate(45deg)}
  .mascot .stage{position:relative;height:176px;width:120px;background:#fff;
    border:1px solid var(--line);border-radius:20px;overflow:hidden;
    box-shadow:0 10px 26px rgba(20,30,60,.13);
    animation:bob 3.8s ease-in-out infinite;transition:transform .25s ease}
  .mascot:hover .stage{transform:scale(1.06)}
  .mascot .stage img{position:absolute;left:50%;bottom:6px;height:calc(100% - 12px);width:auto;
    transform:translateX(-50%);opacity:0;transition:opacity .15s ease}
  .mascot .stage img.on{opacity:1}
  @keyframes bob{0%,100%{translate:0 0}50%{translate:0 -9px}}
  /* 视口不够宽时，正文右侧让出看板娘的位置，避免遮挡表格 */
  @media (max-width:1340px){
    .wrap{padding-right:172px}
    .mascot .stage{height:152px;width:104px}
    .mascot .bubble{max-width:196px;font-size:11px}
  }
  @media (max-width:880px){
    .mascot{display:none}
    .wrap{padding-right:24px}
  }
</style>
</head>
<body>
<div class="wrap">
  <header>
    <h1>__H1__</h1>
    <div class="meta">统计周期 __TITLE__　·　数据范围 __FIRST__ ~ __LAST__　·　生成于 __GEN__</div>
  </header>

  <div class="cards">
    <div class="card hi"><div class="k">累计 Token</div><div class="v">__TOTAL_H__</div>
      <div class="s">__TOTAL__</div></div>
    <div class="card"><div class="k">峰值 Token</div><div class="v">__PEAK_H__</div>
      <div class="s">__PEAK_DAY__</div></div>
    <div class="card"><div class="k">最长聊天时长</div><div class="v">__DUR__</div>
      <div class="s">__DUR_FROM__ → __DUR_TO__</div></div>
    <div class="card"><div class="k">当前连续天数</div><div class="v">__STREAK_CUR__ 天</div>
      <div class="s">最长 __STREAK_BEST__ 天　共活跃 __DAYS__ 天</div></div>
    <div class="card"><div class="k">调用次数</div><div class="v">__CALLS__</div>
      <div class="s">次 LLM 请求</div></div>
  </div>

__HEAT_SECTION__  <section>
    <h2>Token 活动（按天）</h2>
    <div class="chart">__BARS__</div>
  </section>

  <section>
    <h2>按模型</h2>
    <table>
      <thead><tr><th>模型</th><th class="num">调用</th><th class="num">token</th><th class="num">约</th></tr></thead>
      <tbody>__MODEL_ROWS__</tbody>
    </table>
  </section>

  <details class="fold">
    <summary>用量最高的会话<span class="hint">共 __SESS_N__ 个</span></summary>
    <div class="fold-body">
      <table>
        <thead><tr><th></th><th>会话</th><th class="num">token</th><th class="num">约</th></tr></thead>
        <tbody>__SESS_ROWS__</tbody>
      </table>
    </div>
  </details>

  <details class="fold">
    <summary>按天明细　·　按项目<span class="hint">明细</span></summary>
    <div class="fold-body">
      <div class="cols">
        <div>
          <h3 class="mini">按天明细</h3>
          <table>
            <thead><tr><th>日期</th><th class="num">token</th><th class="num">约</th></tr></thead>
            <tbody>__DAY_ROWS__</tbody>
          </table>
        </div>
        <div>
          <h3 class="mini">按项目</h3>
          <table>
            <thead><tr><th>项目目录</th><th class="num">token</th><th class="num">约</th></tr></thead>
            <tbody>__PROJ_ROWS__</tbody>
          </table>
        </div>
      </div>
    </div>
  </details>

  <footer>数据来源：WorkBuddy 本地会话记录（每次调用的输入 / 输出 token）</footer>
</div>

<div class="mascot" id="mascot" title="点我一下">
  <div class="bubble" id="bubble"></div>
  <div class="stage" id="stage">
    <img src="assets/front.png" class="on" alt="DeepSeek 看板娘">
    <img src="assets/left.png" alt="DeepSeek 看板娘 侧视">
    <img src="assets/back.png" alt="DeepSeek 看板娘 背面">
    <img src="assets/right.png" alt="DeepSeek 看板娘 侧视">
  </div>
</div>

<script>
(function(){
  var LINES = __MASCOT_LINES__;
  var views = Array.prototype.slice.call(document.querySelectorAll("#stage img"));
  var bubble = document.getElementById("bubble");
  var mascot = document.getElementById("mascot");
  var li = 0, busy = false, hideTimer = null;

  function say(text){
    bubble.textContent = text;
    bubble.classList.add("show");
    clearTimeout(hideTimer);
    hideTimer = setTimeout(function(){ bubble.classList.remove("show"); }, 4600);
  }
  function show(i){
    views.forEach(function(v, k){ v.classList.toggle("on", k === i); });
  }

  mascot.addEventListener("click", function(){
    if (busy) return;
    busy = true;
    var seq = [1, 2, 3, 0], step = 0;
    var timer = setInterval(function(){
      show(seq[step++]);
      if (step >= seq.length){ clearInterval(timer); busy = false; }
    }, 200);
    say(LINES[li++ % LINES.length]);
  });

  // 进页面先打个招呼
  setTimeout(function(){ say(LINES[li++ % LINES.length]); }, 800);
})();
</script>
</body>
</html>
"""
    repl = {
        "__TITLE__": html.escape(title), "__FIRST__": first, "__LAST__": last,
        "__GEN__": generated,
        "__TOTAL_H__": human(agg["total"]), "__TOTAL__": comma(agg["total"]),
        "__PEAK_H__": human(agg["peak_val"]), "__PEAK_DAY__": agg["peak_day"],
        "__DUR__": fmt_dur(agg["longest_sec"]),
        "__DUR_FROM__": agg["longest_from"], "__DUR_TO__": agg["longest_to"],
        "__STREAK_CUR__": str(agg["streak_cur"]), "__STREAK_BEST__": str(agg["streak_best"]),
        "__DAYS__": str(agg["active_days"]), "__CALLS__": comma(agg["calls"]),
        "__HEAT_SECTION__": heat_section,
        "__MASCOT_LINES__": json.dumps(mascot_lines, ensure_ascii=False),
        "__H1__": "DeepSeek Token 面板" if "DeepSeek" in title else "Token 用量面板",
        "__BARS__": bars or "<p style='color:#6b7280'>暂无数据</p>",
        "__MODEL_ROWS__": model_rows, "__SESS_ROWS__": sess_rows,
        "__SESS_N__": str(len(agg["by_session"])),
        "__DAY_ROWS__": day_rows, "__PROJ_ROWS__": proj_rows,
    }
    for k, v in repl.items():
        html_out = html_out.replace(k, v)
    return html_out


# ---------------------------------------------------------------- 入口

def resolve_range(args):
    now = dt.datetime.now()
    if args.all:
        return "全部历史", lambda w: True
    if args.days:
        start = (now - dt.timedelta(days=args.days - 1)).replace(
            hour=0, minute=0, second=0, microsecond=0)
        return "最近 %d 天" % args.days, lambda w: w >= start
    month = args.month or now.strftime("%Y-%m")
    try:
        y, m = month.split("-")
        start = dt.datetime(int(y), int(m), 1)
    except Exception:
        print("月份格式不对，应该像 2026-09")
        sys.exit(1)
    end = dt.datetime(start.year + (start.month == 12), (start.month % 12) + 1, 1)
    return "%s 年 %s 月" % (y, int(m)), lambda w: start <= w < end


def main():
    ap = argparse.ArgumentParser(description="WorkBuddy token 用量统计")
    ap.add_argument("--month", help="指定月份，如 2026-09")
    ap.add_argument("--all", action="store_true", help="全部历史")
    ap.add_argument("--days", type=int, help="最近 N 天")
    ap.add_argument("--all-models", action="store_true", help="统计全部模型（默认只算 DeepSeek）")
    ap.add_argument("--no-html", action="store_true", help="不生成 HTML 报告")
    ap.add_argument("--open", action="store_true", help="生成后自动打开报告")
    args = ap.parse_args()

    if not os.path.isdir(PROJECTS_DIR):
        print("找不到目录：%s" % PROJECTS_DIR)
        sys.exit(1)

    title, within = resolve_range(args)
    meta = load_session_meta()
    records = [r for r in iter_calls() if within(r["when"])]
    if args.all_models:
        title += "　·　全部模型"
    else:
        records = [r for r in records if is_deepseek(r["model"])]
        title += "　·　仅 DeepSeek"
    agg = aggregate(records)

    print_terminal(agg, title, meta)

    if not args.no_html:
        out = build_html(agg, title, meta)
        tmp = OUT_HTML + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(out)
        os.replace(tmp, OUT_HTML)
        print("  报告已生成：%s" % OUT_HTML)
        if args.open:
            rc = os.system('open "%s"' % OUT_HTML)
            if rc != 0:
                print("  自动打开失败，请手动双击：%s" % OUT_HTML)


if __name__ == "__main__":
    main()
