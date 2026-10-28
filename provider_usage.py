#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""中转站真实用量（GET /v1/usage）—— 钱包余额与实扣费用。

为什么单独一个模块：宠物卡片上的「已花费」原来算的是
「本机日志 token × 模型单价」，那是**挂牌价**，不是钱包真扣的钱。
中转站自己有一个用量接口，直接问它才是真账。

两个必须踩对的地方（都实测过，见 docs/20261028_中转站真实用量接入.md）：

1. **User-Agent 不能留默认值。** urllib 默认发 `Python-urllib/3.x`，
   Cloudflare 直接回 403「Error 1010: Access denied」——请求连鉴权都到不了。
   换成能识别身份的 UA 才能过 WAF。

2. **直连不一定通。** 从中国大陆网络访问 api.aishop520.com 会被地区拦截
   （403 unsupported_region）。本机用了一层本地转发（127.0.0.1:8765，
   Safari 指纹 + 系统代理），`ANTHROPIC_BASE_URL` 就指向它 —— 同一个接口，
   走它才通。所以候选地址里除了直连，还有配置里的 base_url。

本模块只负责「拿到并归一化数据」，不认 HTTP、不碰 UI；
调用方拿 snapshot()，永不阻塞（后台线程刷新 + 磁盘缓存兜底）。
"""

import datetime as dt
import json
import os
import platform
import sqlite3
import threading
import time
import urllib.error
import urllib.request

# 默认直连地址：用户给的接口，也作为兜底候选
DIRECT_URL = "https://api.aishop520.com/v1/usage"

# ⚠️ 不要改成 urllib 默认值，也不要留空 —— Cloudflare 会以 Error 1010 拒绝。
#    带一个真实可识别的身份，对方日志里也能看懂是谁在拉数据。
UA = "WorkBuddyPet/1.1 (+https://github.com/xuejh9-png/deepseek-whale-girl; %s)" % platform.system()

CACHE_DIR = os.path.expanduser("~/.local/share/workbuddy-pet")
CACHE_PATH = os.path.join(CACHE_DIR, "provider-usage.json")

CLAUDE_SETTINGS = os.path.expanduser("~/.claude/settings.json")
CC_DB = os.path.expanduser("~/.cc-switch/cc-switch.db")

TTL_OK = 60.0          # 成功：60 秒内直接复用
TTL_FAIL = 20.0        # 失败：20 秒后重试，别把上游打爆
TIMEOUT = 8.0

_lock = threading.Lock()
_state = {"data": None, "ts": 0.0, "endpoint": None, "error": None, "triedAt": 0.0}
_started = False


# ---------------------------------------------------------------- 凭据

def _claude_settings():
    try:
        with open(CLAUDE_SETTINGS, encoding="utf-8") as fh:
            return (json.load(fh) or {}).get("env") or {}
    except (OSError, ValueError, TypeError):
        return {}


def _cc_switch_provider():
    """CC Switch 的供应商配置；只读，且不需要它常驻。"""
    try:
        # 只读模式：cc-switch 可能正在写库，不要抢写锁
        conn = sqlite3.connect("file:%s?mode=ro" % CC_DB, uri=True, timeout=2.0)
        try:
            row = conn.execute(
                "select settings_config from providers "
                "where app_type='claude' and is_current=1").fetchone()
            if not row:
                row = conn.execute(
                    "select settings_config from providers "
                    "where app_type='claude' order by is_current desc limit 1").fetchone()
        finally:
            conn.close()
        if not row:
            return {}
        return (json.loads(row[0] or "{}") or {}).get("env") or {}
    except (OSError, ValueError, TypeError, sqlite3.Error):
        return {}


def credentials():
    """返回 (base_url, api_key, 来源)。取不到 key 返回 (base, None, ...)。

    优先级：显式环境变量 → 进程环境里的 ANTHROPIC_* →
    ~/.claude/settings.json（Claude Code 真正在用的那把）→ CC Switch 库。
    """
    env = os.environ
    if env.get("PET_USAGE_KEY"):
        return (env.get("PET_USAGE_URL") or DIRECT_URL), env["PET_USAGE_KEY"].strip(), "PET_USAGE_KEY"

    if env.get("ANTHROPIC_AUTH_TOKEN") or env.get("ANTHROPIC_API_KEY"):
        key = (env.get("ANTHROPIC_AUTH_TOKEN") or env.get("ANTHROPIC_API_KEY") or "").strip()
        return env.get("ANTHROPIC_BASE_URL") or "", key, "进程环境"

    conf = _claude_settings()
    key = (conf.get("ANTHROPIC_AUTH_TOKEN") or conf.get("ANTHROPIC_API_KEY") or "").strip()
    if key:
        return conf.get("ANTHROPIC_BASE_URL") or "", key, "~/.claude/settings.json"

    conf = _cc_switch_provider()
    key = (conf.get("ANTHROPIC_AUTH_TOKEN") or conf.get("ANTHROPIC_API_KEY") or "").strip()
    if key:
        return conf.get("ANTHROPIC_BASE_URL") or "", key, "CC Switch 配置"

    return "", None, "未找到 Key"


def _trim(base):
    base = (base or "").strip().rstrip("/")
    if base.endswith("/v1"):
        base = base[:-3]
    return base


def endpoints(base):
    """候选地址：上次成功的 → 配置里的 base → 直连。

    base 常常是本地转发层（127.0.0.1:8765）；它和直连是同一个上游，
    返回结构一致，谁通用谁。
    """
    out = []
    for url in (_state.get("endpoint"), (base.rstrip("/") + "/v1/usage") if base else None, DIRECT_URL):
        if url and url not in out:
            out.append(url)
    return out


# ---------------------------------------------------------------- 取数

class FetchError(Exception):
    def __init__(self, reason, detail=""):
        super().__init__(reason)
        self.reason = reason
        self.detail = detail


def _classify(code, body):
    low = body.lower()
    if "1010" in body or "access denied" in low:
        return "Cloudflare 拦了默认 UA（Error 1010）"
    if "unsupported_region" in low:
        return "地区限制：直连被拒，需走本地转发层"
    if code in (401, 403):
        return "Key 被拒（%d），可能已失效或无权限" % code
    if code == 429:
        return "请求太频繁（429），稍后重试"
    return "接口返回 %d" % code


def _get(url, key):
    req = urllib.request.Request(url, headers={
        "Authorization": "Bearer " + key,
        "User-Agent": UA,                       # 见文件头：这一行不能省
        "Accept": "application/json",
        "Cache-Control": "no-store",
    })
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        body = ""
        try:
            body = exc.read(4096).decode("utf-8", "ignore")
        except OSError:
            pass
        raise FetchError(_classify(exc.code, body), body[:300]) from None
    except (urllib.error.URLError, OSError) as exc:
        raise FetchError("连不上：%s" % getattr(exc, "reason", exc), "") from None
    try:
        data = json.loads(raw.decode("utf-8", "ignore"))
    except ValueError:
        raise FetchError("返回的不是 JSON（可能被网关换成了 HTML）", raw[:120].decode("utf-8", "ignore")) from None
    if not isinstance(data, dict):
        raise FetchError("返回结构不认识", "")
    return data


# ---------------------------------------------------------------- 归一化

def _num(value):
    try:
        out = float(value)
    except (TypeError, ValueError):
        return 0.0
    return out if out == out else 0.0          # NaN 归零


def _bucket(raw):
    """把 today / total / daily 行统一成同一套字段名。

    ⚠️ 明细里的缓存写字段有两种写法：`cache_write_tokens`（daily_usage）
    和 `cache_creation_tokens`（model_stats）。少认一个就会把缓存写算成 0，
    而缓存写恰恰是最贵的一项。
    """
    if not isinstance(raw, dict):
        return None
    write = raw.get("cache_write_tokens")
    if write is None:
        write = raw.get("cache_creation_tokens")
    return {
        "date": raw.get("date"),
        "actualCost": round(_num(raw.get("actual_cost")), 8),
        "listCost": round(_num(raw.get("cost")), 8),
        "accountCost": round(_num(raw.get("account_cost")), 8),
        "requests": int(_num(raw.get("requests"))),
        "tokens": int(_num(raw.get("total_tokens"))),
        "input": int(_num(raw.get("input_tokens"))),
        "output": int(_num(raw.get("output_tokens"))),
        "cacheRead": int(_num(raw.get("cache_read_tokens"))),
        "cacheWrite": int(_num(write)),
    }


def normalize(raw, now=None):
    """把上游 JSON 归一化。字段缺失不许炸 —— 少一个字段也要把有的显示出来。"""
    now = now or dt.datetime.now().astimezone()
    usage = raw.get("usage") if isinstance(raw.get("usage"), dict) else {}

    daily = [d for d in (_bucket(r) for r in (raw.get("daily_usage") or [])) if d]
    models = [m for m in (_bucket(r) for r in (raw.get("model_stats") or [])) if m]
    for m, src in zip(models, raw.get("model_stats") or []):
        m["model"] = src.get("model")

    today = _bucket(usage.get("today")) if usage.get("today") else None
    if not today or not today["requests"]:
        # 上游没给 today 时，用按天明细里本地日期那一行兜底（同一天、同一套口径）
        key = now.date().isoformat()
        today = next((d for d in daily if d["date"] == key), today) or _bucket({})

    total = _bucket(usage.get("total")) if usage.get("total") else None
    if not total or not total["requests"]:
        agg = _bucket({})
        for d in daily:
            for k in ("actualCost", "listCost", "requests", "tokens",
                      "input", "output", "cacheRead", "cacheWrite"):
                agg[k] += d[k]
        total = agg

    month = _bucket({})
    prefix = now.strftime("%Y-%m")
    for d in daily:
        if str(d.get("date") or "").startswith(prefix):
            for k in ("actualCost", "listCost", "requests", "tokens"):
                month[k] += d[k]

    remaining = raw.get("remaining")
    if remaining is None:
        remaining = raw.get("balance")

    return {
        "currency": raw.get("unit") or "USD",
        "planName": raw.get("planName") or "",
        "remaining": round(_num(remaining), 8),
        "valid": raw.get("isValid") is not False,
        "mode": raw.get("mode") or "",
        "today": today,
        "total": total,
        "month": month,
        "daily": daily,
        "models": models,
    }


# ---------------------------------------------------------------- 缓存

def _load_disk():
    try:
        with open(CACHE_PATH, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def _save_disk(data):
    try:
        os.makedirs(CACHE_DIR, mode=0o700, exist_ok=True)
        tmp = CACHE_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False)
        os.replace(tmp, CACHE_PATH)
    except OSError:
        pass                                   # 缓存写不进去不影响主流程


def _wrap(payload, endpoint, key_source, now):
    payload = dict(payload)
    payload.update({
        "ok": True,
        "endpoint": endpoint,
        "keySource": key_source,
        "fetchedAt": now.isoformat(),
        "fetchedTs": now.timestamp(),
    })
    return payload


def refresh(force=False):
    """真正去拉一次。成功返回归一化数据，失败返回 None（并记下原因）。"""
    with _lock:
        if not force and _state["data"] and time.monotonic() - _state["ts"] < TTL_OK:
            return _state["data"]

    if _state.get("endpoint") is None:
        # 上次成功的是哪个地址记在磁盘上：重启后先试它，别每次都先撞地区拦截
        disk = _load_disk()
        if disk and disk.get("endpoint"):
            _state["endpoint"] = disk["endpoint"]

    base, key, source = credentials()
    now = dt.datetime.now().astimezone()
    if not key:
        with _lock:
            _state.update(error="没有找到可用的 Key", triedAt=time.time())
            _state["data"] = _mark_stale(_state["data"])
        return None

    last = None
    for url in endpoints(base):
        try:
            payload = _wrap(normalize(_get(url, key), now), url, source, now)
            with _lock:
                _state.update(data=payload, ts=time.monotonic(), endpoint=url,
                              error=None, triedAt=time.time())
            _save_disk({"endpoint": url, "data": payload})
            return payload
        except FetchError as exc:
            last = "%s（%s）" % (exc.reason, url)
            continue

    with _lock:
        _state.update(error=last, triedAt=time.time(), ts=time.monotonic())
        _state["data"] = _mark_stale(_state["data"])
    return None


def _mark_stale(data):
    if not data:
        return None
    out = dict(data)
    out["stale"] = True
    return out


def snapshot():
    """给调用方的快照：永不阻塞、永不抛异常。

    先用磁盘缓存垫底（宠物重启后第一屏就有数），再返回内存里的最新值。
    """
    with _lock:
        data = _state["data"]
        ts = _state["ts"]
        error = _state["error"]
        if not data:
            disk = _load_disk()
            if disk and isinstance(disk.get("data"), dict):
                data = dict(disk["data"])
                data["stale"] = True
                _state["endpoint"] = disk.get("endpoint") or _state["endpoint"]
                _state["data"] = data
        if not data:
            return None
        out = dict(data)
        out["ageSeconds"] = round(time.time() - (out.get("fetchedTs") or time.time()), 1)
        out["lastError"] = error
        return out


def _loop():
    backoff = TTL_FAIL
    while True:
        ok = refresh(force=True)
        backoff = TTL_OK if ok else min(backoff * 1.5, 300.0)
        time.sleep(backoff)


def start():
    """起一个后台刷新线程。重复调用安全。"""
    global _started
    with _lock:
        if _started:
            return
        _started = True
    threading.Thread(target=_loop, name="provider-usage", daemon=True).start()


# ---------------------------------------------------------------- CLI

def main():
    import argparse
    ap = argparse.ArgumentParser(description="中转站真实用量（钱包余额 / 实扣费用）")
    ap.add_argument("--json", action="store_true", help="输出完整 JSON")
    args = ap.parse_args()
    base, key, source = credentials()
    print("Key 来源：%s%s" % (source, "" if key else "（**没有 Key，接口一定会失败**）"))
    print("候选地址：%s" % " → ".join(endpoints(base) or ["(无)"]))
    data = refresh(force=True)
    if not data:
        print("拉取失败：%s" % (_state.get("error") or "未知原因"))
        return 1
    if args.json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return 0
    t, tot = data["today"], data["total"]
    print("有效期：%s · 口径：%s" % ("正常" if data["valid"] else "已失效", data["planName"] or "—"))
    print("钱包余额   $%.4f" % data["remaining"])
    print("今日实扣   $%.4f（挂牌价 $%.4f · %d 次 · %s token）"
          % (t["actualCost"], t["listCost"], t["requests"], format(t["tokens"], ",")))
    print("累计实扣   $%.4f（挂牌价 $%.4f · %d 次）" % (tot["actualCost"], tot["listCost"], tot["requests"]))
    print("接口地址   %s" % data["endpoint"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
