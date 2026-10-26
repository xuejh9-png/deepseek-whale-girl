# Issue #1 验证记录（2026-10-26）

## 实现与边界

- 今日 Token：WorkBuddy / Codex / Claude Code 独立统计，午夜失效、20 秒缓存、来源覆盖提示。
- Claude 费用：离线导入/导出，明细去重、逐币种核对总额、过滤模型、退款调整、完整历史声明。
  **真实中转站账单尚未接入**，本机实际显示 `已花费 —`；没有用截图金额或官方价格估算。
- 多任务按心跳 sessionId 精确匹配记录文件；稳定 ID、独立状态、轮换观察。
- 保留 160×200 窗口及原动画；费用使用深色实底、白色加粗字。
- 报告移除方格热力图；保留柱状图、明细与重新统计。
- 收紧本机接口 Origin/Host 边界，拒绝外站与 null Origin 读取账单/任务；
  file:// 的报告刷新保留兼容，静态服务不暴露源码与仓库配置。

## 已执行

| 验证 | 结果 |
|---|---|
| `python3 -m unittest -v test_usage` | 22 项通过 |
| `python3 pet_sources.test.py` | 32 项通过 |
| `pet-runtime.test.html`（真实 Chrome） | 35 项通过 |
| `pet-bridge.test.html`（真实 Chrome） | 21 项通过 |
| `pet-wake.test.html`（真实 Chrome） | 11 项通过 |
| `pet-metrics.test.html`（真实 Chrome） | 15 项通过 |
| 隔离浏览器检查：费用格式、任务轮换、过期、覆盖提示、缓存、160×200 边界 | 通过 |
| `python3 -m py_compile usage_stats.py pet_sources.py pet_daemon.py claude_cost.py` | 通过 |
| `node --check web/pet.js` | 通过 |
| Swift 编译及 `zsh desktop/build.sh` | 通过 |
| Mac 真机 `--selftest-runhome` | monotonic=Y, landed=Y, yOK=Y, BAD=0 |
| Mac 实际窗口读取与点击任务按钮 | 通过；费用标签、小字和任务对应状态可见 |
| 报告重新生成（实际代码 + 隔离输出） | 通过，仍有柱状图且没有方格图 |

费用正常值、五笔 Claude、多币种、退款等使用合成账单验证，不代表已核对用户真实中转站账单。
前后截图与过程日志在本机临时目录 `/tmp/whale-issue1/`，属于可删除的验收产物，不是第二份项目。
其中截图金额为合成数据；不提交真实用量、任务文字、配置或账单。

## 本次发现并纠正的回归

初版把 `thread_token_usage` 与 `token_count.info.total_token_usage` 当成同一累计计数交替做差。
真实日志中两者有不同起点，数值交替下降被误认为计数重置，导致严重重复累计。
正在运行的报告服务自动 reload 源码，错误在开发过程中进入了用户的报告。

已先恢复原逐请求统计并重新生成报告，再在隔离候选文件中修正与验证。
最终实现保留按 response_id 去重的单次用量，仅在没有单次记录的轮次中使用独立累计差分。
在同一批真实日志上，与改动前逐请求统计逐会话比较，差额为零。
新增“两套账本存在固定差额”的回归，不能只用两套计数恰好相同的理想数据验证。

## 2026-10-26 16:00 追加：跟随 WorkBuddy、真实刷新、费用口径

用户反馈四件事，逐条对应的根因与修复：

| 反馈 | 根因 | 修复 |
|---|---|---|
| 一直在“睡着啦”，不跟 WorkBuddy | `auto` 只在进程启动那一刻选源；当时 Codex 快照得分更高，之后永远读 Codex | `AutoSource`：每次 `/state` 同时取 WorkBuddy 与 Codex，按“谁在干活 → 谁最后动过 → 状态语义”排序，同分 WorkBuddy 优先 |
| 卡片不会自己刷新 | 全量重扫日志约 3.5 秒，服务端只能把用量缓存 20 秒 | 按文件 `mtime+size` 增量解析（命中约 20ms）；TTL 降到 3 秒，前端 2 秒/3 秒轮询；启动时后台预热 |
| 已花费永远是 `—` | 只认中转站导出账单，而本机从没导入过 | 改为 Claude Code 本机日志 × 模型单价折算；价格表读 `~/.cc-switch/model-pricing.json`，与 CC Switch 面板同口径 |
| 底部状态标签“睡着啦” | 任务状态文字与动画重复，且容易与真实状态混淆 | 删除该标签；任务状态只由角色动画表达 |
| 头部“N 项任务”看不出是谁 | 只报数量，不含身份；用户无法判断是哪个会话 | 先改为项目名，用户明确要求“删掉”，最终**整个任务标签移除**；任务仍参与动画状态选择 |

费用对账（真实日志，但**不记录具体金额与用量**）：

| 口径 | 结果 |
|---|---|
| CC Switch 面板（近 7 天） | 请求数、token 总数、总成本三项 |
| 本实现（同一批可读日志） | 请求数、token 总数与面板**完全一致**；金额差小于 1 美分（四舍五入） |
| 今日部分 | 面板今日柱与实现今日值同量级，请求数一致 |

请求数与 token 数完全一致，说明读的是同一批记录、同一张价目表；金额差异仅来自逐项浮点相加后的四舍五入。
`已花费` 显示累计值，悬停显示今日金额与调用次数（具体数字只在本机窗口里，不写进仓库）。

本轮验证：

| 验证 | 结果 |
|---|---|
| `python3 -m unittest test_usage` | 23 项通过（新增费用分量与家族兜底 1 项） |
| `python3 pet_sources.test.py` | 32 项通过 |
| `pet-runtime.test.html` | 35 项通过 |
| `pet-bridge.test.html` | 21 项通过 |
| `pet-wake.test.html` | 11 项通过 |
| `pet-metrics.test.html` | 19 项通过（新增“不点击也会自动刷新”“无时间戳”“默认三来源合计”） |
| 真机 `/state` | `agent.state=working, source=workbuddy`，`sources` 同时列出 codex=sleeping |
| 真机 `/claude-cost` | `currency=USD`、`estimated=true`、请求数与面板一致 |
| 真机窗口截图 | 已花费有金额、今日 Token 有数值、无时间戳、角色为“敲电脑”而非睡觉 |

仍未覆盖：费用是按模型单价折算，不是中转站实际扣费；WorkBuddy / Codex 的订阅内消耗不计金额。

## 2026-10-26 16:22 追加：删除跑步动作 / 修复「在跑任务却显示空闲」

### 1. 「跑步」动作删除

需求原话：「把跑步的那个动作直接删掉吧先，就是可以备份但是不要出现这个功能了，化繁为简」。

删除的是**整套跑动机制**，不是只隐藏动画：

| 层 | 删除内容 |
|---|---|
| 宿主 `desktop/main.swift` | `runTimer/runFrom/runTo/runT0/runDur/runCycleSeconds`、`runHome()`、`lapAnchor()`、`runDuration()`、`startRun()`、`sendToPage()`、`runSelfTest()`、`case "runCycle"`、⌥⌘H 的 `goHomeOrLap()` |
| 网页 `web/pet.js` | `reportRunCycle()`、`window.petHostEvent` 的 `runStart/runFlip/runEnd` |
| 运行时 `web/pet-runtime.js` | `ACTION_CLIP.runLeft/runRight`、`facing`、`setFacing()`、`applyFacing()` |
| 样式 `web/pet.css` | `transform: scaleX(var(--pet-flip, 1))` 及注释 |
| 清单 | `clips.run`（由 10 个剪辑降为 9 个） |

保留：双击 / 右键 / ⌥⌘H 的**归位**能力，改为 `snapHome()` 一次 `setFrameOrigin` 瞬时到位；
拖动夹边界、位置记忆、换屏回落默认位全部不动。
素材 `assets/pet/run.png`、`assets/run-12-final/` 原样留在磁盘，清单不再引用。
回滚标签 `backup-before-remove-run`，恢复步骤见 `docs/2026-10-26-移除跑步动作.md`。

### 2. 「我在跑任务，宠物还是空闲」根因

用户截图：WorkBuddy 正在处理任务（2m1s），宠物仍是待机姿态。

`/state` 的现场证据：

| 会话 | 心跳年龄 | 会话日志写入年龄 | 实际 |
|---|---|---|---|
| 当前会话 `0fb0ca69`（cwd 本地宠物） | 186.7 s（已"过期"） | **1.3 s** | 正在跑任务 |
| 僵尸会话 `5c437c82`（cwd Investment OS 源码） | **11.0 s（新鲜）** | 604955.8 s ≈ 7 天 | 早已没活动 |

两个叠加的缺陷：

1. `WorkBuddySource._active_sessions` 用 `now - lastHeartbeat < 60s` 判定活跃。
   实测本机 `sessions/*.json` 的 `startedAt / lastHeartbeat / updatedAt` 是**同一秒** ——
   心跳只在会话启动时写一次。于是 60 秒后正在干活的会话被踢出列表。
2. `AutoSource._key` 用 `max(lastActivity, lastHeartbeat)` 打分，心跳因此成为主排序键。
   剩下唯一"心跳新鲜"的僵尸会话永远排第一，卡片长期显示它的「睡着啦」。

修复：

- 活跃判定改为「心跳新鲜 **或** 该会话 jsonl 在 60 秒内被写过」——
  日志写入才是"真的在干活"的证据。
- `AutoSource._key` 与 `WorkBuddySource.snapshot()` 的排序都只看 `lastActivity`，
  心跳完全不参与打分。

### 3. 本轮验证

| 验证 | 结果 |
|---|---|
| `python3 pet_sources.test.py` | **34 项通过**（新增"心跳停了但日志在写"、"僵尸心跳不压活跃会话"） |
| `python3 -m unittest test_usage` | 23 项通过 |
| `pet-runtime.test.html`（Chrome headless） | 30 项通过 |
| `pet-bridge.test.html`（Chrome headless） | 14 项通过（run 断言改为"清单已无 run"） |
| `pet-wake.test.html` | 11 项通过 |
| `pet-metrics.test.html` | 16 项通过 |
| `zsh desktop/build.sh` | 通过 |
| `--selftest-home` | `from=(940,115) end=(1260,75) home=(1260,75) landed=Y BAD=0` |
| 真机 `/state` | `state=working, label=执行中, source=workbuddy, cwd=当前工作区`；僵尸会话仍在列表但为 `sleeping`、`heartbeatFresh=true` 且不再抢占 |
| 真机截屏（放大核对） | 角色为「坐着敲电脑」的 work 剪辑，不再是待机站姿 |

仍未覆盖：宿主端"双击归位是不是真的瞬间完成"没有自动化断言（`--selftest-home` 只验落点）；
归位路径已无任何定时器与动画调用，实际观感仍需用户手点一次确认。

## 尚未验证 / 限制

- 无真实完整账单，未验证与中转站后台累计消费的实际金额对账；导出完整性依赖账单声明。
- Mac 原生完整鼠标拖拽、双击、长按退出未全部手工验收。电脑控制工具对部分原生坐标操作返回
  `AXError.notImplemented`。网页手势回归与真机自动复位通过，不将其替代完整手工验收。
- 本机自然日按系统时区；只有本机可读日志的用量可统计，不等同账户全部设备用量。
- 本轮改动已提交并推送；任务开始前的既有改动一并入库，范围见 CURRENT_TASK.md。

### 4. 归位删除（同日 16:38 追加）

用户：「为什么双击它还是会回到这个右下角？这个功能也删掉」。

`homeOrigin()` 的定义就是**屏幕右下角**（鼠标所在屏的右下角），所以"双击归位"表现为
"双击就瞬移到右下角"—— 这不是 bug，是设计；跑步删掉后归位只剩这一个作用，用户不需要。

删除范围：

| 位置 | 内容 |
|---|---|
| `desktop/main.swift` | `snapHome()`、`case "reset"`、⌥⌘H 注册、`snapHomeSelfTest()`、`--selftest-home` 入口 |
| `web/pet.js` | 右键分支 `button === 2 → tell('reset')`、双击识别 `lastClickAt` / `isDbl` |
| `pet-bridge.test.html` | 「右键/双击发出 reset」改为「不再触发归位」 |

保留：拖动移动、位置记忆、拖动夹边界、长按 0.8 秒退出菜单、⌥⌘Q 退出。

验证：`zsh desktop/build.sh` 通过；`pet-bridge.test.html` 13 项通过
（断言改为"右键不含 reset""双击不含 reset 且仍有 clicked"）；
源码中 `reset` / `snapHome` / `lastClickAt` 计数均为 0。

仍未覆盖：双击手感（是否完全无位移）需用户手点确认；自动化只能证明没有发出归位指令。

### 5. 卡片全空（2026-10-26 16:50 追加）

现象：宠物窗口位置正确、页面能画出来，但 `已花费 —`、`今日 Token —`、角色一直 idle。
同一地址 `http://127.0.0.1:8791/pet.html` 在 Chrome 里完全正常（`$0.22` / `1.0亿` / `working`）。

取证链：

| 步骤 | 结果 |
|---|---|
| 宿主日志加 `webView.url` | `http://127.0.0.1:8791/pet.html` —— 不是 file:// |
| 给 daemon 加受控请求日志 `PET_LOG_REQUESTS=1` | 请求确实到达：`GET /state`、`/usage`、`/claude-cost` |
| 403 原因拆解 | `DENY /state origin=null` |

根因：`loadPage()` 先用 `loadFileURL` 显示本地页，探到端口后再 `webView.load(http)`。
WKWebView 的安全上下文停在 file 那次，之后页面里所有 `fetch` 都带 `Origin: null`，
被 `pet_daemon._local_request` 按"opaque origin 不能读私有接口"拒绝。
以前没暴露是因为竞态：file 还没提交就被 http 顶掉，安全上下文没来得及建立。

修复：

- `loadPage()` 先做一次裸 socket 探测（loopback 立即返回），端口通就直接 `loadHTTP()`，
  **完全不加载本地文件**；
- 只有独立模式兜底才 `loadFileURL`；之后服务起来要升级，必须 `installWebView()`
  重建实例并挂回窗口 —— 只换 URL 不换实例，Origin 仍是 null；
- `pet_daemon` 的 403 记录具体原因（Host / origin=null / cross-site），默认安静，
  `PET_LOG_REQUESTS=1` 打开。

验证：

| 验证 | 结果 |
|---|---|
| `--selftest-save`（位置记忆） | 通过 |
| 冷启动（daemon 未运行 → 宿主拉起 → 第 2 次探测成功） | 日志出现"从本地文件切到 HTTP → 重建 webView" |
| 服务端请求 | `/state`、`/usage`、`/claude-cost` 全部 200，无 403 |
| 真机截屏 | `已花费 $0.22`、`今日 1.1亿 Token`、角色为敲电脑姿态 |
| `pet-bridge.test.html` / `pet-runtime.test.html` / `pet-wake.test.html` / `pet-metrics.test.html` | 13 / 30 / 11 / 16 项通过 |
| `python3 -m unittest test_usage` / `pet_sources.test.py` | 23 / 34 项通过 |
| 源码残留检查 | `web/pet.js` 中 `reset`、`snapHome`、`lastClickAt` 均为 0；`JSERR` 诊断代码已移除 |

限制：`Origin: null` 只在 WKWebView 出现，网页回归（Chrome）抓不到这一层，
只能靠"宿主日志 + 服务端 403 原因 + 真机截屏"三件一起验。
