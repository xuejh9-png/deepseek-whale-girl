# 桌面宠物 · Windows 11 版

同一只鲸鱼娘，外壳从 macOS 的 Swift + WKWebView 换成 Electron。
动画、素材、交互逻辑与 macOS 版**共用同一份前端代码**（`../pet.html`、`../web/`、`../assets/pet/`），
只有窗口宿主和取数层是各自实现的。

---

## 先说清楚：什么验证过、什么没验证

这一版是在 **macOS 上写完并跑通的**，没有 Windows 11 真机可用。所以：

| 项目 | 状态 | 说明 |
|---|---|---|
| 数据层（状态 / 今日 token / 花费） | ✅ 已验证 | `node test-claude-code.js` **31 项**断言；与 macOS 版逐项对账，金额 0.224 / 5 次调用 / 82641 token 完全一致 |
| 前端复用（动画、双击反应、卡片） | ✅ 已验证 | 同一份 `web/` 代码，在 Electron 里实际跑过 |
| 窗口创建 / 位置记忆 / 菜单 / 快捷键 | ✅ 已验证（macOS 上跑 Electron） | 实际启动后窗口正常渲染、数据加载成功，见下方「本机实跑记录」 |
| **Windows 11 真机表现** | ⚠️ **未验证** | 透明窗口、鼠标穿透、任务栏隐藏、多屏缩放都只在 macOS 上跑过 |
| 打包成 .exe 安装包 | ⚠️ 未验证 | 需要在 Windows 上执行 `npm run dist`，脚本已配好 |

**别把"没报错"当成"Windows 上一定正常"**。下面「已知风险」列了大概率要调的地方。

---

## 本机实跑记录（macOS 上验证 Electron 版）

```
[pet] 本地服务 http://127.0.0.1:57252
[pet] 数据目录 /Users/c0437/.claude/projects
```

| 接口 | 实测返回 |
|---|---|
| `/health` | `{"ok":true,"version":"win-1.0","source":"claude"}` |
| `/state` | `state=working, source=workbuddy`（与 macOS 版同一结论：取活跃的那个源） |
| `/claude-cost` | `amount=0.224, requests=5` —— 与同一台机器上 Python 版**逐项相同** |
| `/usage` | `totalTokens=162,997,022`（WorkBuddy 133.8M + ChatGPT 29.2M + Claude 12,957），`complete=true` |
| `/pet.html` | 200，1661 字节 |

窗口侧：透明背景生效（能看见桌面）、角色动画在跑、卡片显示「已花费 $0.22 / 今日 1.6亿 Token」。

> 这些都是在 **macOS 上跑 Electron** 得到的。Windows 11 的窗口行为（透明、穿透、任务栏）
> 仍然是未验证项 —— Electron 的 API 一样，但驱动和 DWM 不一样。

---

## 它读什么

三处本地日志，和 macOS 版同口径：

| 来源 | Windows 路径 | 读什么 |
|---|---|---|
| WorkBuddy | `C:\Users\<你>\.workbuddy\projects\*\*.jsonl` | `providerData.rawUsage` 的 prompt / completion |
| ChatGPT | `C:\Users\<你>\.codex\sessions\YYYY\MM\DD\*.jsonl` | `token_usage_record`，按 `response_id` 去重 |
| Claude Code | `C:\Users\<你>\.claude\projects\**\*.jsonl` | `message.usage`，按 `message.id` 去重 |

对应三种显示：

- **今日 token（底部）**：三个来源分别统计再相加；点一下轮换单来源，悬停看精确数字
- **已花费（顶部）**：只有 Claude Code —— 另外两家的日志里没有价格信息，算不出钱
- **动画状态**：WorkBuddy 和 Claude Code 各取一份，选「最活跃 + 最后动过」的那个

**目录不存在就跳过**，不报错也不编数字，缺失的来源显示成 `—`。
所以哪怕只装了其中一个工具，其余部分照常工作。

路径可以覆盖：

```bat
set PET_CLAUDE_DIR=D:\somewhere\.claude\projects
set PET_CLAUDE_PRICING=D:\pricing.json
```

价目表优先读 CC Switch 的 `~/.cc-switch/model-pricing.json`；
没有就用内置的 Claude 公开价目（Fable / Opus / Sonnet / Haiku，4.x–5.x）。

---

## 怎么跑

### 方式一：源码直接跑（需要 Node 18+）

```bat
cd windows
npm install
npm start
```

### 方式二：打成安装包（在 Windows 上执行）

```bat
cd windows
npm install
npm run dist
```

产物在 `windows\dist\`（NSIS 安装包）。想要免安装的绿色版：

```bat
npm run dist:portable
```

> `npm run dist` **必须在 Windows 上跑**。在 macOS/Linux 上交叉打包 Windows 安装包
> 需要 wine，官方也不推荐，不保证可用。

---

## 操作

| 操作 | 效果 |
|---|---|
| 拖动她 | 移动位置，松手就记住 |
| 双击她 | 角色反应 |
| 长按 0.8 秒 | 弹出菜单（只有「退出」）|
| **Ctrl + Alt + Q** | 退出 |

> Ctrl+Alt+Q 不是装饰：无边框窗口没有关闭按钮，鼠标穿透判定万一失效，
> 这是唯一还能用的退出口。

---

## 和 macOS 版差在哪

| | macOS 版 | Windows 版（本目录） |
|---|---|---|
| 窗口宿主 | Swift + WKWebView（`desktop/main.swift`） | Electron（`main.js`） |
| 数据服务 | Python `pet_daemon.py`，常驻进程 | Node 内置 HTTP 服务（`server.js`），随窗口启停 |
| 数据源 | WorkBuddy + ChatGPT + Claude Code | 三个都接，与左边一致 |
| 依赖 | Python 3 + Swift 命令行工具 | 打包后无依赖；源码跑需要 Node |
| 安装包体积 | 约 200 KB（原生二进制） | **约 90–120 MB**（Electron 运行时） |
| 退出快捷键 | ⌥⌘Q | Ctrl+Alt+Q |

---

## 已知风险（Windows 真机首跑请重点看这几条）

1. **透明窗口可能变黑底**
   部分 Intel 核显 + 旧驱动的机器上，`transparent: true` 会退化成黑色矩形。
   如果出现，先更新显卡驱动；仍不行就说明这台机器需要换方案（原生 Win32 分层窗口）。

2. **鼠标穿透的判定区域**
   Electron 的坐标原点在**左上**，macOS 的 Cocoa 在**左下**。
   `main.js` 里 hitbox 的纵向区间已经翻过来（19%~95%），但视觉上是否符合预期，
   需要真机点一点：角色身上该能点，留白处该穿透。

3. **高 DPI / 多显示器**
   `setPosition` 用的是 DIP 坐标。多屏不同缩放比例时，拖动可能出现偏移或跳动，
   属于需要真机调的部分。

4. **任务栏不显示图标**
   `skipTaskbar: true` 是刻意的（她不该占任务栏）。副作用是任务管理器之外没有入口，
   所以退出快捷键必须有。

5. **杀毒软件误报**
   Electron 打包的未签名 exe，Windows Defender / 360 有可能拦截。
   要么加白名单，要么自己签个名（需要证书）。

---

## 代码结构

```
windows/
  main.js              窗口宿主：透明置顶窗、鼠标穿透、拖动、位置记忆、菜单、快捷键
  hitbox.js            命中区判定（Y 轴原点翻转单独抽出来，好回归）
  preload.js           把网页用的 window.webkit.messageHandlers.petHost 接到 Electron IPC
  server.js            内置 HTTP 服务：/state /usage /claude-cost + 静态资源
  claude-code.js       Claude Code：读日志 → 状态 / 花费（与 macOS 版同口径）
  sources.js           三源合一：今日 token 合计、WorkBuddy 状态、合并状态
  pricing.js           价目表 + 型号匹配（与 claude_cost.py 同一份数据）
  test-claude-code.js  回归：状态 / 去重 / 计价 / 命中区，纯 Node 跑，不需要 Electron
  package.json         electron-builder 配置（extraResources 把 ../web 和 ../assets/pet 打进去）
```

为什么要 shim 一层 `window.webkit.messageHandlers.petHost`：
网页是照 WKWebView 的接口写的，有了同名对象就能两个平台跑同一份前端，
不用维护两套动画与交互代码。

---

## 回归

```bat
cd windows
node test-claude-code.js
```

不依赖 Electron，也不依赖本机此刻是否在用 Claude Code（用合成日志 + 受控时间戳）。
