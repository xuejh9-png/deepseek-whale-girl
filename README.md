# WorkBuddy Token 用量面板 + 桌面宠物

两个本地小工具，都从一个数据源出发：**WorkBuddy 的本地会话记录**。

1. **用量面板** —— 把 WorkBuddy 里消耗的 token 统计出来，生成一个网页报告
2. **桌面宠物** —— 一只用透明窗口浮在桌面上的小鲸鱼娘，会**看着你干活**：
   你让她工作时她坐着敲电脑、思考时歪头、任务完成时跳起来、没动静了会睡着

![状态](docs/pet-states.png)

> 顺带说明：**这不是 WorkBuddy 官方功能**，是一个独立的本地小工具，
> 只读 WorkBuddy 写在磁盘上的记录文件，不改它、也不联网。

---

## 它解决什么问题

WorkBuddy 客户端**只显示积分（credits），不显示 token 数**。
但你想知道"这个月到底用了多少 token"的时候，客户端答不了。

好在这件事的原始数据一直在本地：每次 LLM 调用的 token 明细，
都写在 `~/.workbuddy/projects/<项目>/<会话>.jsonl` 里。
所以完全可以自己算出来 —— 这个项目就是把这件事做成能看的东西。

---

## 用量面板

![用量面板](docs/report.png)

```bash
python3 usage_stats.py              # 本月（默认只统计 DeepSeek）
python3 usage_stats.py --all        # 全部历史
python3 usage_stats.py --days 7     # 最近 7 天
python3 usage_stats.py --all-models # 不过滤模型
python3 usage_stats.py --open       # 生成后自动打开
```

macOS 上也可以**双击 `查看用量.command`**。

### 口径

`累计 Token = sum(total_tokens) = 输入 + 输出`，和 OpenAI / Codex 一致。

这个口径是**对照 Codex 本地记录验证过**的：

```json
// ~/.codex/sessions/**/rollout-*.jsonl 里的 token_usage_record
{"input_tokens": 26960, "cached_input_tokens": 16128, "output_tokens": 370,
 "total_tokens": 27330}          // 26960 + 370 = 27330 —— 缓存部分不额外剔除
```

所以两边算出来的数字**可以直接横向比**。

---

## 桌面宠物

```bash
双击 启动桌面宠物.command
```

- 透明、无边框、置顶的小窗口，**角色以外的区域会穿透鼠标** —— 不挡你操作
- 拖她可以移动位置；**按住不动 0.8 秒**或**右键**唤出菜单
- **⌥⌘Q**（Option+Command+Q）退出
- 默认**只看到她**，没有卡片、没有数字面板

### 她在反映什么

宠物的动作是**真数据驱动**的，不是随机动画：

| 她在做什么 | 说明 |
|---|---|
| 待机 | 没有任务 |
| 歪头思考 | 模型正在推理 |
| 坐着敲电脑 | 正在调用工具 / 执行任务 |
| 跳起来 + 小星星 | 任务完成 |
| 愣住 + 头顶 `!` | 出错了 |
| 睡着了 | 长时间没有活动 |
| 被拖起悬空 | 你正在拖她 |

状态推断的规则（全部读本地记录，不猜测）：

| 记录里的活动 | 推断 |
|---|---|
| `function_call` / `function_call_result` | 执行中 |
| `reasoning` | 思考中 |
| `user` 消息 | 等你回复 |
| `assistant` 消息 | 刚完成 |
| 无活跃会话心跳 | 未连接 |

想亲眼确认联动：双击 **`查看宠物状态.command`**，
它会每秒打印一次「状态 / 宠物在播哪个剪辑 / 当前任务」。

### 支持的状态与动作

10 个动画剪辑，各自是 6 列行优先的透明 Sprite Sheet：

| 剪辑 | 帧 | 类型 |
|---|---|---|
| `idle` | 20 | 循环（含呼吸、眨眼、摆尾） |
| `run` | 12 | 循环（两步跑步周期） |
| `jump` | 14 | 一次性（含落地压缩） |
| `click` | 8 | 一次性 |
| `drag` | 12 | 开场 4 帧 + 循环 8 帧 |
| `think` | 12 | 开场 4 + 循环 8 |
| `work` | 12 | 开场 4 + 循环 8 |
| `success` | 14 | 一次性 |
| `error` | 12 | 一次性 |
| `sleep` | 12 | 开场 8 + 循环 4 |

---

## 目录结构

```
usage_stats.py          用量统计 + 生成报告页
pet_daemon.py           本地状态服务（推断 Agent 状态 + 聚合用量）
pet.html                桌面宠物页面（只有角色）
web/pet-runtime.js      动画运行时：Sprite 取帧 / 状态机 / 动作队列
web/pet.js              胶水层：拉状态 + 指针事件 + 宿主通信
desktop/main.swift      Swift 透明窗口宿主（无边框 / 置顶 / 鼠标穿透 / 全局快捷键）
assets/pet/             角色素材（10 个 Sprite Sheet + manifest.json）

verify-assets.py        素材逐帧验收（几何 / 循环接缝 / 透明度）
intake-assets.py        素材自动接收入库（只自动新增，永不覆盖已有素材）
pet-runtime.test.html   动画运行时回归测试（28 项）
```

### 依赖

- **Python 3**：统计与状态服务只用标准库
  （素材验收脚本额外需要 `pillow` + `numpy`）
- **Swift**：编译桌面宠物宿主（macOS 需装命令行工具 `xcode-select --install`）
- 桌宠本身是 WKWebView，不需要 Node

---

## 一些设计取舍

- **不写假数据**：取不到的字段显示 `—`，不编造
- **不做 Dashboard**：宠物只负责"让你知道 AI 大概在干什么"，
  详细的用量数字都在面板页，不往宠物身上堆
- **素材自动接入只做新增，永不覆盖**：
  这条是被一次真实的数据覆盖事故逼出来的 ——
  一个只判断"来源与本地是否不同"的自动化，在本地比来源更新时会**必然**造成回退
- **颜色约定**：涨用红、跌用绿（A 股习惯）

---

## 已知问题

- `error` 剪辑的 12 帧里实际只有约 6 个不同姿态（前 3 帧完全相同），
  反应感偏弱，待返工
- 宠物只支持 macOS（窗口宿主用了 Cocoa + WKWebView）
- `用量面板` 目前只统计 DeepSeek 系列

---

## License

MIT
