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
| `查看用量.command` | 双击入口，macOS 终端菜单式运行 |
| `用量报告.html` | 生成物。数据快照，会在每次运行时覆盖 |
| `assets/*.png` | 看板娘四视图（正面 / 左侧 / 背面 / 右侧） |

## 看板娘

页面右下角的鲸鱼娘，**点一下会原地转一圈，并说一句当前的真实数据**。

- 四个视图循环切换：front → left → back → right
- 台词从实际统计结果生成（累计 token、峰值日、连续天数、调用次数等）
- 待机时有轻微上下浮动

素材由用户提供的四视图设定图裁切而来（见 `assets/`）。

## 注意

- 数字是**活的**：每次运行都会包含最新数据，所以两次运行结果会略有差异。
- `用量报告.html` 是生成物，直接改它没用，改 `usage_stats.py` 后重新生成。
