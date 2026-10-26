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
