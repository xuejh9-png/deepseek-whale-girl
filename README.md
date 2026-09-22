# WorkBuddy 用量面板 + 桌面宠物

两个**互相独立**的模块，只共享一份只读的数据源。

```
WorkBuddy 本地记录（只读）
~/.workbuddy/projects/*/*.jsonl     逐次调用的 token 明细 + Agent 活动序列
~/.workbuddy/sessions/*.json        活跃会话心跳
        │
        ├──→ usage_stats.py  ──→ 用量报告.html      纯用量面板（无宠物）
        │
        └──→ pet_daemon.py   ──→ /state   ──→ 桌面宠物（独立页面）
                                 /usage  ──→ 用量数据（供报告页/诊断用）
```

**报告页是工作界面，桌宠是 AI Companion —— 两者不重叠。**

---

## 一、用量报告

```bash
python3 usage_stats.py              # 本月（默认只算 DeepSeek）
python3 usage_stats.py --all        # 全部历史
python3 usage_stats.py --days 7     # 最近 7 天
python3 usage_stats.py --all-models # 不过滤模型
python3 usage_stats.py --open       # 生成后自动打开
```

或双击 `查看用量.command`。

### 数据来源与口径

`~/.workbuddy/projects/<项目>/<会话>.jsonl` 里每条 LLM 调用都带 `providerData.rawUsage`
（`prompt_tokens` / `completion_tokens` / `total_tokens`），是**逐次调用的完整记账**。

口径：`累计 Token = sum(total_tokens) = 输入 + 输出`，与 OpenAI / Codex 一致。
对照 Codex 本机记录（`~/.codex/sessions/**/rollout-*.jsonl` 的 `token_usage_record`）验证过：
`total_tokens = input_tokens + output_tokens`，缓存部分不额外剔除。两者可直接横向比较。

⚠️ 两个**不能**用的数据源：
- `~/.workbuddy/traces/` 的 `trace.totalTokens` —— 覆盖率仅 13%，会低估约 6.5 倍
- `workbuddy.db` 的 `session_usage` 表 —— 记的是积分和上下文，不是 token

---

## 二、桌面宠物

```bash
双击 启动桌面宠物.command
```

会自动带起状态服务（`pet_daemon.py`），首次运行还会自动编译 Swift 宿主。

- 窗口**透明 + 无边框 + 置顶**，角色以外的区域**穿透鼠标**
- 拖动移动位置；**右键**选择置顶 / 退出
- 默认**只看到角色**，没有 Token / Context / Model / 卡片

### 状态来源（真实数据，不造假）

`pet_daemon.py` 读会话记录的活动序列推断：

| 记录类型 | 状态 |
|---|---|
| `function_call` / `function_call_result` | 执行中 |
| `reasoning` | 思考中 |
| `user` 消息 | 等你回复 |
| `assistant` 消息 | 刚完成 |
| 无活跃会话心跳 | 未连接 |

### 接口

| 端点 | 用途 |
|---|---|
| `GET /state` | **只回 Agent 状态**（宠物用） |
| `GET /usage` | 用量数据（报告页 / 未来诊断页用） |
| `GET /pet.html` | 桌面宠物页面 |
| `GET /` | 用量报告 |

---

## 三、文件

| 文件 | 说明 |
|---|---|
| `usage_stats.py` | 用量统计 + 生成报告页 |
| `pet_daemon.py` | 本地状态服务：Agent 状态推断 + 用量聚合 |
| `pet.html` · `web/pet.css` · `web/pet.js` | 桌面宠物页面（只含角色） |
| `desktop/main.swift` · `build.sh` | Swift + WKWebView 透明窗口宿主 |
| `启动桌面宠物.command` | 一键启动桌宠 |
| `查看用量.command` | 双击查看用量 |
| `用量报告.html` | 生成物，每次运行覆盖 |
| `assets/pet/` | 宠物素材（当前为占位，待新 Asset Pack 替换） |
| `assets/gen2/` | 角色早期渲染留档 |
| **`ASSET_SPEC.md`** | **运行时契约**：换素材时必须同步改的参数与验证流程（内部用） |

---

## 四、注意

- 数字是**活的**：每次运行都包含最新数据，两次结果会略有差异。
- `用量报告.html` 是生成物，改它没用，改 `usage_stats.py` 后重新生成。
- 报告页含会话标题等本地信息，**推 GitHub 前留意仓库可见性**。
- `desktop/WorkBuddyPet.app` 是编译产物，不进仓库（`build.sh` 会重新生成）。
- **角色素材的外发规格书在 `~/Desktop/WorkBuddy桌宠_素材规格_给GPT.md`**
  （自包含版本，可直接交给生成模型）。本仓库的 `ASSET_SPEC.md` 只记运行时契约，
  两份内容不重复。
