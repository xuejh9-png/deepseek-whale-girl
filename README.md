# WorkBuddy Token 用量面板 + 桌面宠物

两个本地小工具，都从一个数据源出发：**WorkBuddy 的本地会话记录**。

1. **用量面板** —— 把 WorkBuddy 里消耗的 token 统计出来，生成一个网页报告
2. **桌面宠物** —— 一只用透明窗口浮在桌面上的小鲸鱼娘，会**看着你干活**：
   你让她工作时她坐着敲电脑、思考时歪头、任务完成时跳起来、没动静了会睡着

![状态](docs/pet-states.png)

> 顺带说明：**这不是 WorkBuddy 官方功能**，是一个独立的本地小工具，
> 只读 WorkBuddy 写在磁盘上的记录文件，不改它、也不联网。

---

## 30 秒上手

| 想干什么 | 怎么做 |
|---|---|
| 看这个月用了多少 token | 双击 **`查看用量.command`**（或 `python3 usage_stats.py --open`） |
| 让鲸鱼娘浮在桌面上 | 双击 **`启动桌面宠物.command`** —— 首次会自动编译窗口，约 10 秒 |
| 只想看看她的动画 | 双击 **`pet.html`** —— 不用装任何东西，也不需要 WorkBuddy |

环境要求：**macOS + Python 3**（统计与状态服务只用标准库）。
只有编译桌面窗口那一步才需要 Swift 命令行工具：`xcode-select --install`。

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

### 菜单与「她丢不了」

右键 / 长按唤出的菜单：

| 菜单项 | 说明 |
|---|---|
| 当前：执行中 · 帮我改个函数 | **只读**一行，直接告诉她此刻在干什么（含当前任务名） |
| 回到初始位置 | 她**跑**回右下角（真的跑，不是瞬移） |
| 置顶显示 / 取消置顶 | 切换是否压在其他窗口之上 |
| 退出桌面宠物 | 等同 ⌥⌘Q |

两条防丢机制 —— 桌宠最大的可用性问题其实是**找不回来**：

- **拖不出屏幕**：拖动时至少留 48×56px 在屏幕内，不可能被拖到看不见的地方
- **记住位置**：关掉再打开回到你上次放她的地方，不再跳回右下角。
  换显示器 / 改分辨率后旧坐标会失效，这时自动回落到默认位（并写日志说明原因）

> 跑回原位用的是 `run` 剪辑。向左跑时会按 manifest 的 `flipForLeft`
> **水平翻转**，否则会变成"倒着滑"。

### 不用 WorkBuddy 也能跑

宠物本体**不依赖 WorkBuddy**。克隆下来直接双击 `pet.html` 就能看到一只
会呼吸、眨眼、摆尾的鲸鱼娘（10 个动作全都可用，不是只有待机）。

想要**桌面宠物**（浮在桌面上，而不是留在浏览器标签里）就编译窗口宿主：

```bash
zsh desktop/build.sh          # 需要 swiftc（xcode-select --install）
open desktop/WorkBuddyPet.app
```

宿主启动时先显示本地页面（不等网络），再探测本地状态服务；
**探测不到就保持独立模式** —— 她照常活着，只是不会跟着 Agent 状态变。

### 她在反映什么

宠物的动作是**真数据驱动**的，不是随机动画：

| 她在做什么 | 说明 |
|---|---|
| 待机 | 没有任务 |
| 歪头思考 | 模型正在推理 |
| 坐着敲电脑 | 正在调用工具 / 执行任务 |
| 跳起来 + 小星星 | 任务完成 |
| 楞住 + 头顶 `!` | 出错了 |
| 睡着了 | 长时间没有活动 |
| 被拖起悬空 | 你正在拖她 |

想亲眼确认联动：双击 **`查看宠物状态.command`**，
它会每秒打印一次「状态 / 宠物在播哪个剪辑 / 当前任务」。

### 状态源（可以换成别的工具）

宠物运行时只认一个契约，**状态从哪来是独立的一层**：

```jsonc
// GET /state
{
  "source": "workbuddy",
  "agent": {
    "state": "working",        // idle|thinking|working|success|waiting|error|sleeping|offline
    "label": "执行中",
    "task": "帮我改个函数",      // 可选
    "cwd": "/path/to/project",  // 可选
    "lastActivity": 1790067331119
  }
}
```

仓库里已带三个适配器（`pet_sources.py`）：

| 名字 | 读什么 |
|---|---|
| `workbuddy` | `~/.workbuddy/projects/*.jsonl` + 会话心跳 |
| `codex` | `~/.codex/sessions/**/rollout-*.jsonl` |
| `none` | 不读任何东西 —— 宠物只做自己的 idle 动画 |

```bash
python3 pet_daemon.py --list          # 看本机哪些可用
python3 pet_daemon.py                 # auto：挑此刻真的有活动的那个
python3 pet_daemon.py --source none   # 独立模式
```

**接自己的工具**：在 `pet_sources.py` 里加一个类，实现 `snapshot()` 返回上面的
`agent` 结构即可 —— 运行时、窗口、动画一行都不用改。
`pet_sources.test.py` 里有现成的测试写法（用合成记录 + 受控时间戳，
不依赖真实数据就能测活跃状态）。

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
pet_daemon.py           本地状态服务（薄核心，只负责 HTTP）
pet_sources.py          状态源适配器（workbuddy / codex / none …）
pet.html                桌面宠物页面（只有角色）
web/pet-runtime.js      动画运行时：Sprite 取帧 / 状态机 / 动作队列
web/pet.js              胶水层：拉状态 + 指针事件 + 宿主通信
desktop/main.swift      Swift 透明窗口宿主（无边框 / 置顶 / 鼠标穿透 / 全局快捷键）
assets/pet/             角色素材（10 个 Sprite Sheet + manifest.json / manifest.js）

verify-assets.py        素材逐帧验收（几何 / 循环接缝 / 透明度 / 可见性下限）
intake-assets.py        素材自动接收入库（只自动新增，永不覆盖已有素材）
build-manifest-js.py    manifest.json → manifest.js（让 file:// 也能读到）
pet-runtime.test.html   动画运行时回归测试（33 项）
pet-bridge.test.html    宿主 ↔ 网页 桥的回归测试（12 项）
pet_sources.test.py     状态推断回归测试（13 项）
```

### 回归怎么跑

前两个测试是网页，**直接双击用浏览器打开**即可（不需要 daemon），
结果写在页面标题里：`RESULT|PASS=n|FAIL=n|ALL_OK`。

窗口宿主（Swift）那边另有两项自检，不需要用鼠标点菜单就能验：

```bash
desktop/WorkBuddyPet.app/Contents/MacOS/WorkBuddyPet --selftest-runhome
#   挪开一段距离再跑回初始位置，打印轨迹后退出
#   期望：monotonic=Y landed=Y yOK=Y BAD=0

desktop/WorkBuddyPet.app/Contents/MacOS/WorkBuddyPet --selftest-save
#   写入一次当前位置并退出，用于验证「程序自己写、程序自己读」的位置记忆
```

> ⚠️ **不要用 `defaults write … -array 400 220` 去造位置数据** ——
> 它会把数字存成**字符串**，验到的不是真实链路。要造数据就用 `--selftest-save`。

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
- **每条"放弃"的路径都要留下日志**：
  静默 `return nil` / `return` 会让故障变成一个查不出的现象。
  「位置记不住」这一条就是靠日志才发现真正原因是数据格式不对，而不是逻辑错
- **测试要自足**：网页回归测试不该依赖本机有没有跑状态服务。
  之前 `pet-bridge.test.html` 会真去连 `127.0.0.1:8791`，
  在没跑服务的机器上结果不同，而且定时器一直有活干会让无头浏览器**永不退出**
- **颜色约定**：涨用红、跌用绿（A 股习惯）

---

## 已知问题

- `error` 剪辑的 12 帧里实际只有约 6 个不同姿态（前 3 帧完全相同），
  反应感偏弱，待返工
- 宠物只支持 macOS（窗口宿主用了 Cocoa + WKWebView）
- `用量面板` 目前只统计 DeepSeek 系列

> 关于验收：`verify-assets.py` 会同时回答两个问题 ——
> **「有没有变」**（原画布上数变化的像素个数）和 **「看不看得见」**
> （按实际显示尺寸再量一遍，下限 40px）。
> 只回答前者的话，那种"做了但做废了"的动画会在验收表上全绿、而在桌面上不动 ——
> `work` 就是这么溜过第一版验收的。

---

## License

MIT
