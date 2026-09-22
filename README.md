# DeepSeek Token 用量面板

统计本机 WorkBuddy 里 DeepSeek 模型的 token 消耗，生成一个带互动看板娘的网页面板。

## 为什么需要它

WorkBuddy 客户端只显示**积分（credits）**，不显示 token 数。但每次调用的 token 明细
都存在本地会话记录里，所以可以自己算出来，而且口径能和 Codex 直接对比。

## 数据来源

```
~/.workbuddy/projects/<项目目录名>/<会话ID>.jsonl
```

每条 LLM 调用记录里带 `providerData.rawUsage`：

```json
{
  "prompt_tokens": 37847,
  "completion_tokens": 160,
  "total_tokens": 38007,
  "prompt_tokens_details": {"cached_tokens": 0}
}
```

单个会话一个文件，48/49 个文件都有 usage 记录——**这是逐次调用的完整记账**。

### 口径

`累计 Token = sum(total_tokens) = 输入 + 输出`。

这是 OpenAI / Codex 的标准口径。对照 Codex 本机记录
（`~/.codex/sessions/**/rollout-*.jsonl` 里的 `token_usage_record`）验证过：

```json
{"input_tokens": 26960, "cached_input_tokens": 16128, "output_tokens": 370,
 "total_tokens": 27330}     // 26960 + 370 = 27330，缓存不额外剔除
```

两者口径一致，可直接横向比较。

## 用法

```bash
python3 usage_stats.py                # 本月（默认只算 DeepSeek）
python3 usage_stats.py --all          # 全部历史
python3 usage_stats.py --days 7       # 最近 7 天
python3 usage_stats.py --month 2026-09
python3 usage_stats.py --all-models   # 不过滤，统计全部模型
python3 usage_stats.py --open         # 生成后自动用浏览器打开
```

双击 **`查看用量.command`** 也可以（终端菜单选 1/2/3）。

脚本零依赖，只用 Python 标准库。

## 文件说明

| 文件 | 说明 |
|---|---|
| `usage_stats.py` | 主脚本：读本地记录 → 聚合 → 出终端表格 + HTML 面板 |
| `pet_daemon.py` | 本地状态服务：推断 Agent 状态、聚合真实 token，`GET /state` |
| `web/pet.css` · `web/pet.js` | 悬浮宠物：拖拽 / 状态机 / Token HUD / 点击面板 |
| `desktop/main.swift` · `build.sh` | 桌面宠物窗口（Swift + WKWebView，透明无边框） |
| `启动桌面宠物.command` | 一键启动桌面宠物（自动带状态服务、首次自动编译） |
| `查看用量.command` | 双击入口，终端菜单式查看用量 |
| `用量报告.html` | 生成物。数据快照，会在每次运行时覆盖 |
| `assets/pet/*.png` | 宠物四视图（正面 / 左侧 / 背面 / 右侧），透明背景 |
| `assets/gen2/*.png` | 角色原始渲染留档（白底，未抠图） |

## 宠物系统

### Web 宠物（报告页内）

右下角的鲸鱼娘是**透明背景的悬浮角色**，不是卡片。

- **拖动**：按住她可以拖到任意位置，位置记在 `localStorage`；
  至少 60% 始终留在可视区内
- **点击**：弹出轻量面板（模型 / Context / 本月用量 / 调用次数 / 当前任务），再点关闭
- **悬停**：HUD 从「● 36%」展开成「358,063 / 1M · DeepSeek-V4.1-Flash」
- **状态**：`空闲 / 思考中 / 执行中 / 刚完成 / 等你回复 / 未连接`，
  由真实数据驱动，头顶显示对应的特效（思考点 / 旋转环 / 对勾 / 感叹号）
- **空闲时**每 9~18 秒会左右张望一次

### 桌面宠物

脱离浏览器，独立悬浮在桌面上：

```bash
双击 启动桌面宠物.command
```

窗口是**无边框 + 透明 + 置顶**的，角色以外的区域会**穿透鼠标**，
不影响你操作别的东西。拖动方式同上，**右键**选择置顶 / 退出。

首次运行会自动编译（约 10 秒，需要 `swiftc`）。

### 状态是怎么来的

`pet_daemon.py` 读的是 WorkBuddy 的会话记录，按活动序列推断：

| 记录类型 | 状态 |
|---|---|
| `function_call` / `function_call_result` | 执行中 |
| `reasoning` | 思考中 |
| user 消息 | 等你回复 |
| assistant 消息 | 刚完成 |
| 无活跃会话 | 未连接 |

取不到的数据一律显示 `—`，**不造假**。

## 注意

- 数字是**活的**：每次运行都会包含最新数据，所以两次运行结果会略有差异。
- `用量报告.html` 是生成物，直接改它没用，改 `usage_stats.py` 后重新生成。
- `用量报告.html` 里含会话标题等本地信息，**推 GitHub 前注意仓库可见性**。

