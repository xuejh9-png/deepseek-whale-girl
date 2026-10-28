# 当前任务：Issue #1

需求：https://github.com/xuejh9-png/deepseek-whale-girl/issues/1
以及该 Issue 的最终费用显示补充。用户要求阅读项目并实施；后续明确要求修正错误用量，
并提升“已花费”在非白色背景上的可读性。

## 范围

- Claude Code 用量接入；全工具、本机自然日的今日 Token 与来源覆盖。
- Claude 中转站实际账单导入/导出、核对、去重、覆盖范围；未接入显示 `—`。
- WorkBuddy 按心跳 ID 关联多任务；单只桌宠轮换观察，不控制或终止任务。
- 160×200 窗口内的费用/Token/任务文字；保留既有手势与动画。
- 删除报告方格热力图，保留按天柱状图、明细、重新统计。

不在范围：宠物素材重做、网站重建、官方单价估算费用、发布到 GitHub、生产账单写入。

## 比较基准与目标

开始时：`main`，HEAD `87b4fda`。现分支 `codex/claude-usage-tasks`。
开始前 README.md、usage_stats.py、查看用量.command 已有未提交修改，尤其用量面板已有大幅扩展；
不能把相对 HEAD 的全部 diff 都归因于本次任务。既有改动保留；本次新增变更仍在工作区。
本次修改文件：usage_stats.py、pet_sources.py、pet_daemon.py、claude_cost.py、pet.html、
web/pet.js、web/pet.css、desktop/main.swift、test_usage.py、pet-metrics.test.html、README.md、本文件及验证记录。

## 验收

1. 真实 Codex 日志与原逐请求统计逐会话对账一致；两套累计口径存在差额时不重复加历史。
2. 三来源今日统计、跨午夜、重放、缓存字段、缺失用量覆盖提示正确。
3. 三任务可轮换，完成、心跳失效、文件缺失/改名、重现互不串会话。
4. 五笔 Claude 与其他模型混合账单只计 Claude；重复、退款、多币种及部分历史处理明确。
5. 原 API 字段和动画手势兼容，费用有清晰底色；报告刷新不复活方格图。
6. 未接入真实账单、未验证的手工操作必须明确报告，不把合成账单当真实金额。

验证详情：docs/issue-1-validation.md。

## 2026-10-26 16:00 接续（用户口头追加，已完成）

1. 删除底部“睡着啦”状态标签；任务状态只由角色动画表达。
   头部“N 项任务”同样删除（用户明确要求），卡片只保留已花费与今日 Token 两项。
2. 已花费换色：暖白底 + 深蓝字，取自立绘本身的配色。
3. 已花费改为 **Claude Code 本机消费**（模型单价折算），与 CC Switch 用量面板同口径；
   不再等中转站导出账单，账单存在时仍以账单为准。
4. “假卡片”根因修复：`auto` 从“启动时定源”改为“每次请求重新比较”；用量按文件增量解析，
   TTL 20s → 3s；前端 2s/3s 轮询，启动时后台预热。
5. 不显示更新时间，只保留自动刷新。
6. 底部栏明确为**今日全部工具** Token；点击仍可轮换单来源。

本轮改动文件：pet_sources.py、pet_daemon.py、usage_stats.py、claude_cost.py、pet.html、
web/pet.js、web/pet.css、pet-metrics.test.html、test_usage.py、README.md、本文件、验证记录。
验证结果见 docs/issue-1-validation.md 的「2026-10-26 16:00 追加」一节。

## 2026-10-26 16:22 接续（用户口头追加，已完成）

1. **删除「跑步」动作**（用户：「直接删掉，可以备份但不要出现这个功能，化繁为简」）。
   删除范围：宿主 `runHome/lapAnchor/runDuration/startRun/sendToPage/runSelfTest/runCycle`、
   网页 `petHostEvent`、运行时 `runLeft/runRight/facing/flipForLeft`、CSS `--pet-flip`、
   清单 `clips.run`。
   **保留**归位能力与防丢机制，归位改为 `snapHome()` 一次 `setFrameOrigin` 瞬时到位。
   素材 `assets/pet/run.png`、`assets/run-12-final/` 未删；回滚标签 `backup-before-remove-run`；
   恢复说明 `docs/2026-10-26-移除跑步动作.md`。
2. **修复"我在跑任务、宠物还是空闲"**。根因：WorkBuddy 的 `sessions/*.json` 心跳
   **只在会话启动时写一次**，`_active_sessions` 用 `now - heartbeat < 60s` 判定，
   一分钟后正在干活的会话被踢出列表；剩下唯一"心跳还新鲜"的是一个七天没活动的僵尸会话
   （`5c437c82`，cwd=Investment OS 源码），于是卡片显示睡着/空闲。
   修复：活跃判定改为「心跳新鲜 **或** 会话日志 60 秒内被写过」；
   `AutoSource._key` 与队列排序只看 `lastActivity`，心跳不再参与打分。

本轮改动文件：desktop/main.swift、web/pet.js、web/pet-runtime.js、web/pet.css、
assets/pet/manifest.json、assets/pet/manifest.js、pet-bridge.test.html、pet-runtime.test.html、
pet_sources.py、pet_sources.test.py、README.md、docs/2026-10-26-移除跑步动作.md、本文件、验证记录。
验证结果见 docs/issue-1-validation.md 的「2026-10-26 16:22 追加」一节。

## 2026-10-26 16:38 接续（用户口头追加，已完成）

用户：「为什么双击它还是会回到这个右下角？这个功能也删掉」。

原因：归位落点就是 `homeOrigin()` = 鼠标所在屏幕的右下角。跑步删掉后归位只剩"换位置"作用。

删除：`snapHome()`、`case "reset"`、⌥⌘H 注册、`snapHomeSelfTest()`、`--selftest-home`、
网页右键分支与双击识别（`lastClickAt`/`isDbl`）。
现在：拖动 = 移动；双击 = 点击反应；右键 = 无动作；长按 0.8 秒 = 退出菜单；⌥⌘Q = 退出。

改动文件：desktop/main.swift、web/pet.js、pet-bridge.test.html、README.md、
docs/2026-10-26-移除跑步动作.md、本文件、验证记录。

## 2026-10-26 16:50 接续（同日第三轮，已完成）

**1. 卡片一直显示 `已花费 —` / `今日 Token —`、角色一直 idle**

根因：宿主先把 `file://` 页面显示出来，探到状态服务后再 `load(http)`。
WKWebView 的安全上下文停在 file 那次，之后页面里所有 fetch 都带 `Origin: null`，
被 `pet_daemon` 的 Origin 检查 403（服务端日志实测 `DENY /state origin=null`）。
之前是竞态：有时 file 还没提交就被 http 顶掉，看着正常。

修复：
- `loadPage()` 先探一次端口（裸 socket，立刻返回），通了直接 `loadHTTP()`，完全不碰 file；
- 确需从 file 升级时 `installWebView()` **重建实例**，只换 URL 不够；
- `pet_daemon` 的 403 现在会记录具体原因（`PET_LOG_REQUESTS=1` 时），不再只有一句话。

顺带确认：启动日志里"窗口起点"变化是用户手动拖动后记忆的位置，不是 bug；
位置记忆与拖动夹边界未改动。

改动文件：desktop/main.swift、pet_daemon.py、web/pet.js、README.md、本文件、验证记录。

## 2026-10-28 18:00 接续（中转站真实用量，已完成）

用户转达中转站客服：桌宠不要再用 CC Switch 的配置取数，直接调
`GET https://api.aishop520.com/v1/usage`（`remaining`、`usage.today.actual_cost`、
`usage.total.actual_cost`、`daily_usage`、`model_stats`），并解释为什么费用和后台不一致。

1. 新增 `provider_usage.py`：Key 取自 `~/.claude/settings.json` / CC Switch 库；
   带固定 UA（默认 `Python-urllib` 会被 Cloudflare 以 Error 1010 拒绝，实测）；
   地址顺序「上次成功 → base（本地转发层 127.0.0.1:8765）→ 直连」（直连在国内被
   `unsupported_region` 拦，实测）；60 秒缓存 + 磁盘兜底 + 后台线程，调用方永不阻塞。
2. `claude_cost.spend_snapshot()` 顺序改为 **中转站实扣 → 导入账单 → 本机折算**；
   `/claude-cost` 增加余额、挂牌价、请求数；`/usage` 的 Claude 一栏换成中转站口径
   （替换不累加），新增 `/provider-usage` 排障口。
3. 对账（2026-10-28）：本机 76 次 / 18.83M token / 折算 $11.97；
   中转站 129 次 / 23.38M token / 挂牌 $25.39 / 实扣 $19.48。
   差额来自覆盖（本机只有 13 个 jsonl、累计 81 次 vs 197 次）、分量计数、以及
   "挂牌价≠实扣且不是固定倍率"。详见 docs/20261028_中转站真实用量接入.md。
4. 测试：provider_usage.test.py 19 项、test_usage.py 23 项、pet_sources.test.py 34 项全过。

注意：本次只改数据源与展示，未改窗口/手势/素材；`pet_daemon` 需重启才生效。
