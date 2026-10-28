"""中转站用量接入的契约测试 —— 全部用合成数据，不联网、不读真实 Key。"""
import datetime as dt
import json
import os
import tempfile
import unittest
from unittest.mock import patch

import claude_cost as C
import pet_daemon as D
import provider_usage as P

# 站内接口的真实结构（字段名照抄，数值合成）
SAMPLE = {
    "balance": 180.89683851,
    "remaining": 180.89683851,
    "isValid": True,
    "mode": "unrestricted",
    "planName": "钱包余额",
    "unit": "USD",
    "usage": {
        "today": {"actual_cost": 19.48221153, "cost": 25.3893405,
                  "cache_creation_tokens": 3528185, "cache_read_tokens": 19280510,
                  "input_tokens": 483374, "output_tokens": 87752,
                  "requests": 129, "total_tokens": 23379821},
        "total": {"actual_cost": 20.10316149, "cost": 26.6250375,
                  "cache_creation_tokens": 3713160, "cache_read_tokens": 19775185,
                  "input_tokens": 508257, "output_tokens": 94383,
                  "requests": 197, "total_tokens": 24090985},
    },
    "daily_usage": [
        {"date": "2026-10-22", "requests": 9, "input_tokens": 1056, "output_tokens": 960,
         "cache_read_tokens": 48188, "cache_write_tokens": 52623, "total_tokens": 102827,
         "cost": 0.2878291, "actual_cost": 0.33658959},
        {"date": "2026-10-28", "requests": 129, "input_tokens": 483374, "output_tokens": 87752,
         "cache_read_tokens": 19280510, "cache_write_tokens": 3528185, "total_tokens": 23379821,
         "cost": 25.3893405, "actual_cost": 19.48221153},
    ],
    "model_stats": [
        {"model": "claude-opus-5-5", "requests": 189, "input_tokens": 501433,
         "output_tokens": 93954, "cache_creation_tokens": 3706691,
         "cache_read_tokens": 19759555, "total_tokens": 24061633,
         "cost": 26.587801, "actual_cost": 20.09074134, "account_cost": 15.772906848},
        {"model": "claude-sonnet-5-5", "requests": 7, "input_tokens": 6776,
         "output_tokens": 349, "cache_creation_tokens": 6462,
         "cache_read_tokens": 15574, "total_tokens": 29161,
         "cost": 0.0363118, "actual_cost": 0.01214274, "account_cost": 0.004924746},
    ],
}


def reset(tmpdir):
    """每个用例从干净状态开始，且永不写真实缓存目录。"""
    P._state.update({"data": None, "ts": 0.0, "endpoint": None, "error": None, "triedAt": 0.0})
    P.CACHE_PATH = os.path.join(tmpdir, "provider-usage.json")


class NormalizeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        reset(self.tmp.name)
        self.now = dt.datetime(2026, 10, 28, 18, 0, tzinfo=dt.timezone(dt.timedelta(hours=8)))

    def tearDown(self):
        self.tmp.cleanup()

    def test_today_and_total(self):
        d = P.normalize(SAMPLE, self.now)
        self.assertEqual(d["remaining"], 180.89683851)
        self.assertEqual(d["currency"], "USD")
        self.assertEqual(d["today"]["actualCost"], 19.48221153)
        self.assertEqual(d["today"]["listCost"], 25.3893405)
        self.assertEqual(d["total"]["requests"], 197)

    def test_cache_write_field_name_differs(self):
        """daily_usage 用 cache_write_tokens，model_stats 用 cache_creation_tokens。"""
        d = P.normalize(SAMPLE, self.now)
        self.assertEqual(d["daily"][-1]["cacheWrite"], 3528185)
        self.assertEqual(d["models"][0]["cacheWrite"], 3706691)

    def test_today_falls_back_to_daily_row(self):
        raw = json.loads(json.dumps(SAMPLE))
        raw.pop("usage")
        d = P.normalize(raw, self.now)
        self.assertEqual(d["today"]["requests"], 129)
        self.assertEqual(d["today"]["actualCost"], 19.48221153)

    def test_total_falls_back_to_daily_sum(self):
        raw = json.loads(json.dumps(SAMPLE))
        raw["usage"].pop("total")
        d = P.normalize(raw, self.now)
        self.assertEqual(d["total"]["requests"], 138)
        self.assertAlmostEqual(d["total"]["actualCost"], 0.33658959 + 19.48221153, places=6)

    def test_month_only_counts_current_month(self):
        raw = json.loads(json.dumps(SAMPLE))
        raw["daily_usage"].append({"date": "2026-09-30", "requests": 1, "actual_cost": 5.0,
                                   "cost": 5.0, "total_tokens": 10})
        d = P.normalize(raw, self.now)
        self.assertEqual(d["month"]["requests"], 138)      # 9 月那笔不算

    def test_garbage_does_not_raise(self):
        for raw in ({}, {"usage": None}, {"remaining": "abc", "daily_usage": [None, 1]},
                    {"balance": None, "model_stats": "nope"}):
            d = P.normalize(raw, self.now)
            self.assertIn("today", d)
            self.assertEqual(d["today"]["requests"], 0)


class EndpointTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        reset(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_user_agent_is_not_python_default(self):
        """默认 UA 会被 Cloudflare 以 Error 1010 拒绝，这一行是硬要求。"""
        self.assertTrue(P.UA.strip())
        self.assertNotIn("python-urllib", P.UA.lower())
        self.assertNotIn("Python-urllib", P.UA)

    def test_candidates_trim_and_dedupe(self):
        P._state["endpoint"] = "http://127.0.0.1:8765/v1/usage"
        got = P.endpoints("http://127.0.0.1:8765/v1/")
        self.assertEqual(got[0], "http://127.0.0.1:8765/v1/usage")   # 上次成功的排第一
        self.assertEqual(got.count("http://127.0.0.1:8765/v1/usage"), 1)
        self.assertIn(P.DIRECT_URL, got)

    def test_falls_through_to_next_endpoint(self):
        calls = []

        def fake_get(url, key):
            calls.append(url)
            if "aishop520" in url:
                raise P.FetchError("地区限制")
            return SAMPLE

        with patch.object(P, "credentials", return_value=("http://127.0.0.1:8765", "sk-test", "test")), \
             patch.object(P, "_get", side_effect=fake_get):
            data = P.refresh(force=True)
        self.assertEqual(len(calls), 1)                    # 第一个就成，不该继续撞
        self.assertEqual(data["endpoint"], "http://127.0.0.1:8765/v1/usage")
        self.assertTrue(data["ok"])

    def test_all_endpoints_fail_marks_error_and_keeps_last_value(self):
        with patch.object(P, "credentials", return_value=("", "sk-test", "test")), \
             patch.object(P, "_get", side_effect=P.FetchError("地区限制：直连被拒")):
            self.assertIsNone(P.refresh(force=True))
        self.assertIn("地区限制", P._state["error"])
        self.assertIsNone(P.snapshot())                    # 没有旧值就是 None，不编数

    def test_disk_cache_survives_restart(self):
        with patch.object(P, "credentials", return_value=("", "sk-test", "test")), \
             patch.object(P, "_get", return_value=SAMPLE):
            P.refresh(force=True)
        P._state.update({"data": None, "ts": 0.0})         # 模拟重启
        snap = P.snapshot()
        self.assertIsNotNone(snap)
        self.assertTrue(snap["stale"])                      # 磁盘里的值一律标 stale
        self.assertEqual(snap["remaining"], 180.89683851)

    def test_no_key_does_not_raise(self):
        with patch.object(P, "credentials", return_value=("", None, "未找到 Key")), \
             patch.object(P, "_get", side_effect=AssertionError("没有 Key 不该发请求")):
            self.assertIsNone(P.refresh(force=True))
        self.assertIn("Key", P._state["error"])


class ClassifyTests(unittest.TestCase):
    def test_cloudflare_1010_vs_region(self):
        self.assertIn("1010", P._classify(403, "<title>Error 1010: Access denied</title>"))
        self.assertIn("地区", P._classify(403, '{"code":"unsupported_region"}'))
        self.assertIn("Key", P._classify(401, "{}"))


class CostAdapterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        reset(self.tmp.name)
        self.now = dt.datetime(2026, 10, 28, 18, 0, tzinfo=dt.timezone(dt.timedelta(hours=8)))
        self.payload = dict(P.normalize(SAMPLE, self.now),
                            ok=True, endpoint="http://127.0.0.1:8765/v1/usage",
                            fetchedAt=self.now.isoformat(), fetchedTs=0.0)

    def tearDown(self):
        self.tmp.cleanup()

    def test_provider_snapshot_shape(self):
        with patch.object(P, "snapshot", return_value=self.payload):
            d = C.provider_snapshot(self.now)
        self.assertEqual(d["amount"], 20.1032)              # 累计实扣，不是挂牌价
        self.assertEqual(d["today"]["USD"], "19.4822")
        self.assertEqual(d["remaining"], 180.8968)
        self.assertFalse(d["estimated"])
        self.assertTrue(d["verified"])

    def test_spend_prefers_provider_over_local_estimate(self):
        with patch.object(P, "snapshot", return_value=self.payload):
            d = C.spend_snapshot(now=self.now)
        self.assertEqual(d["source"], "中转站 /v1/usage（实际扣费）")
        self.assertEqual(d["amount"], 20.1032)

    def test_spend_falls_back_when_provider_down(self):
        with patch.object(P, "snapshot", return_value=None), \
             patch.object(C, "iter_claude_calls", return_value=iter(())), \
             patch.object(C, "price_table", return_value={}):
            d = C.spend_snapshot(path=os.path.join(self.tmp.name, "nope.json"), now=self.now)
        self.assertTrue(d.get("estimated"))
        self.assertIn("接口不可达", d["note"])


class UsageMergeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        reset(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def snap(self):
        return {"totalTokens": 30, "complete": True,
                "bySource": {"workbuddy": {"totalTokens": 10, "calls": 1},
                             "codex": {"totalTokens": 20, "calls": 2},
                             "claude": {"totalTokens": 1000, "calls": 5}},
                "coverage": {"workbuddy": "connected", "codex": "connected", "claude": "connected"},
                "note": "已接入三种来源（本机可读日志）",
                "dayLocal": "2026-10-28"}

    def test_claude_replaced_not_added(self):
        payload = {"ok": True, "ageSeconds": 3.0,
                   "today": {"tokens": 23379821, "requests": 129, "actualCost": 19.48},
                   "total": {"tokens": 24090985, "requests": 197, "actualCost": 20.10},
                   "month": {"tokens": 0, "requests": 0, "actualCost": 0.0},
                   "remaining": 180.9, "daily": [], "models": [], "currency": "USD"}
        with patch.object(P, "snapshot", return_value=payload):
            out = D.apply_provider_today(self.snap())
        self.assertEqual(out["bySource"]["claude"]["totalTokens"], 23379821)
        self.assertEqual(out["bySource"]["claude"]["calls"], 129)
        self.assertEqual(out["bySource"]["claude"]["localTokens"], 1000)   # 本地值留证据
        self.assertEqual(out["bySource"]["workbuddy"]["totalTokens"], 10)
        self.assertEqual(out["totalTokens"], 23379821 + 30)                 # 不是 23379821 + 1030
        self.assertEqual(out["coverage"]["claude"], "provider")

    def test_original_snapshot_not_mutated(self):
        original = self.snap()
        with patch.object(P, "snapshot", return_value={"ok": True,
                                                       "today": {"tokens": 5, "requests": 1}}):
            D.apply_provider_today(original)
        self.assertEqual(original["bySource"]["claude"]["totalTokens"], 1000)
        self.assertEqual(original["totalTokens"], 30)

    def test_provider_down_keeps_local_numbers(self):
        with patch.object(P, "snapshot", return_value=None):
            out = D.apply_provider_today(self.snap())
        self.assertEqual(out["totalTokens"], 30)


if __name__ == "__main__":
    unittest.main(verbosity=2)
