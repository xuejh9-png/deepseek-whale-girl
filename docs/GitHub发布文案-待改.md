# GitHub 发布文案（草稿 · 等你改）

> 用法：你直接在**这份文件里改**，改完告诉我，我把它落到 README 和仓库设置里。  
> 我下面用【】标出每个字段填到 GitHub 的哪个位置。

---

## 【一】仓库名（GitHub 上那个网址的最后一段，定了就不好改）

**宠物定名：deepseek 鲸鱼娘**（2026-09-24 用户确认）

仓库名候选（挑一个）：

| 候选 | 说明 |
| --- | --- |
| `deepseek-whale-girl` | 直译她，网址里也好认（**建议**）|
| `whale-girl-companion` | 「伙伴」，弱化厂商名，公开时风险小 |
| `workbuddy-companion` | 强调用途（用量看板 + 宠物），不点名 AI 厂商 |

> ⚠️ **公开前值得想一下**：仓库名/描述里直写 `deepseek`，等于把别人的品牌名放进你的项目名。
> 自己玩没事，**公开**的话有可能被要求改名。想稳用 `whale-girl-companion`；想直白用 `deepseek-whale-girl`。**你定。**

---

## 【二】仓库描述（About 那一栏，一句话）

> GitHub 上显示在仓库名右边、About 框里的那行字。建议 ≤60 字。

**中文版（建议主用这句）**

```
把我在 WorkBuddy 里的 token 用量做成看得懂的报表，因为我只用deepseek所以最后就改成deepseek看板了哈哈哈，并养一只会跟着工作状态变化的鲸鱼娘。纯本地运行，不联网、不需要账号。
```

**英文版（想加就加，很多仓库中英各一行）**

```
A local-only macOS desktop companion for WorkBuddy: a live token-usage dashboard plus a pet whose mood mirrors what you're doing.
```

---

## 【三】Topics（标签，About 框下面一排小方块）

```
workbuddy  desktop-pet  macOS  swift  webkit  token-usage  dashboard  wkwk
```

（前三四个最重要，GitHub 靠它们做推荐。）

---

## 【四】README 的开头（仓库首页正文，最显眼的一段）

> 现在的 README 是一份**技术流水账**（改了什么、验收多少条），  
> 适合我们自己看，**不适合第一次来的人**。  
> 下面这版专为"第一次点进来的人"写，你改完我替换掉现在 README 的开头部分。

---

### 建议的新开头

```markdown
# deepseek 鲸鱼娘

一个跑在自己电脑上的鲸鱼娘：**把 WorkBuddy 的 token 用量变成看得懂的报表，
再养一只会跟着你工作状态变化的桌宠。**

- **纯本地**：所有数据只从本机的 WorkBuddy 记录里读，不联网、不上传、不需要账号
- **看得见**：本月/本月到今天花了多少、缓存命中多少、哪几个会话最费
- **有陪伴感**：小鲸鱼会跟着你在忙 / 在等 / 出错 / 完成，换成对应的动作

## 30 秒上手（macOS）

1. 双击 `desktop/WorkBuddyPet.app` —— 宠物就出来了
2. 想看用量：双击根目录的 `查看用量.command`
3. 想关掉宠物：`⌥⌘Q`（Option + Command + Q）

宠物窗口是**穿透点击**的，不会挡住你干活；`⌥⌘H` 是把它叫回身边。

## 它长什么样

（这里放一张截图 —— 建议截一张"宠物在屏幕角落 + 用量面板"的同框图）

## 它是怎么知道我在忙的

（一句话讲清：读本机 WorkBuddy 的会话记录 → 推断现在处于什么状态 → 宠物换动作。
能写清"不联网、不读聊天内容"这两点最好。）

## 目录里有什么

| 目录 | 是什么 |
|---|---|
| `desktop/` | 桌面宠物本体（Swift + WKWebView）|
| `web/` | 宠物的动画与状态机（HTML/CSS/JS）|
| `assets/pet/` | 精灵图素材（各状态的帧表）|
| `docs/` | 设计文档与验收记录 |

## 已知问题

（**这一节必须有**。写清当前没做完/有瑕疵的地方，
并注明是"已知"还是"回归"——否则下一个人会以为是坏了。）

## 许可

见 `LICENSE`。
```

---

## 【五】需要你补的两处（我写不了）

1. **一张截图**：README 里"它长什么样"那节要配图。你截一张  
   **宠物 + 用量面板同框**的图给我，我放进仓库并调好尺寸。
2. **"已知问题"那节的口径**：有些毛病是**设计上就接受**的（比如长裙遮住腿、  
   跑步只有一小截腿可见），有些是**真没做完**的。你想怎么写，我在旁边帮你分清。

---

## 【六】上传的步骤（等体检过了再走，先给你看着）

> 走之前**必须先跑一遍体检**（凭据扫描、敏感文件是否真的没进版本库、  
> 有没有把家目录绝对路径写进去、README 截图是否过期）。  
> 这一步我下一轮会做，**体检没过就不推**。

**① 你在浏览器点一次（只有这一步需要你的账号）**

打开这个链接建仓（仓库名用上面选的）：

```
https://github.com/new?name=workbuddy-companion&visibility=private
```

⚠️ **三个初始化选项全部留空**：不要勾 README、不要勾 .gitignore、不要选 License  
—— 本地已经有这些文件了，GitHub 再生成一份会让推送被拒。

**② 剩下的我在终端做**（你不用敲）

```bash
git remote set-url origin git@github.com:xuejh9-png/workbuddy-companion.git
git ls-remote origin            # 探测远端
git push -u origin main
```

**③ 先 Private，你看一眼满意再公开** —— 公开之前我还会再跑一次凭据扫描。  
（Private 犯错还有救，Public 没有。）
