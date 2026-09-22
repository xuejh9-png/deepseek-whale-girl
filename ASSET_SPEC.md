# ASSET_SPEC — 运行时契约（内部）

> **外发给生成模型的素材规格不在这里。**
> 完整 brief（角色身份 / 视觉风格 / 比例 / 逐剪辑帧相位 / 英文 prompt / 验收）在：
>
> ```
> ~/Desktop/WorkBuddy桌宠_素材规格_给GPT.md
> ```
>
> 本文件只记录**代码实际依赖的契约** —— 换素材时对着这份改，不会漏掉运行时那一侧。

---

## 一、代码依赖的几何参数

改素材几何时，以下位置**必须同步**，否则角色会错位或大小不对：

| # | 位置 | 参数 | 当前值 |
|---|---|---|---|
| 1 | `web/pet.css` `:root` | `--pet-char-height` | `128px` |
| 2 | `web/pet.css` `:root` | `--pet-char-in-canvas` | `0.64` |
| 3 | `web/pet.css` `:root` | `--pet-aspect` | `0.8` |
| 4 | `desktop/main.swift` | `winW` / `winH` | `160` / `200` |
| 5 | `desktop/main.swift` | 鼠标命中区（`updatePassthrough`） | `x23% / y7% / w54% / h70%` |

推导关系（别手算，改 1–3 即可）：

```
--pet-canvas-height = --pet-char-height / --pet-char-in-canvas     (= 200px)
窗口宽             = --pet-canvas-height × --pet-aspect             (= 160px)
窗口高             = --pet-canvas-height                            (= 200px)
```

**素材侧对应值**：画布 320×400，角色 256px（64%），锚点 `(160, 360)`，
显示缩放 0.5×。选 320×400 / 0.5× 是为了在 2× Retina 上
`128 CSS px = 256 设备像素 = 画布角色高度`，像素级对齐最清晰。

---

## 二、代码依赖的文件约定

| 项 | 约定 |
|---|---|
| 目录 | `assets/pet/` |
| 命名 | `<clip>.png`，小写；如 `idle.png` / `think.png` |
| 每张图 | 一个剪辑的水平条带，每行最多 6 帧，帧间无间隙 |
| 单帧 | 320 × 400，RGBA 透明 |
| 清单 | `assets/pet/manifest.json`（帧数 / FPS / 循环 / `airborneFrame` / `introFrames`） |
| 当前状态 | **只有一个占位文件 `front.png`**（静态展示，非最终素材） |

`manifest.json` 的字段约定见外发 brief 第 11 节。

---

## 三、占位素材的现状（重要）

`assets/pet/front.png` 是**旧的角色设计**：

- 来源是早期生成的"自然站姿"稿，体型约 **2.5~3 heads**
- 只做过「改显示尺寸」+「重排进 320×400 画布」，**没有用 CSS 伪装头身比**
- 用途仅限：把 Runtime、尺寸、锚点、窗口几何、鼠标穿透调通

**头身比（1.82 heads）等新素材到位后才能达标。**
runtime 侧不会用 `scale` / `transform` 去修比例 —— 那只会让画面变形。

---

## 四、换素材后的验证流程

素材放进 `assets/pet/` 之后，按顺序跑：

```bash
# 1. 起服务（必须与后续 curl / 截图在同一条命令里：后台进程会随 shell 结束）
python3 pet_daemon.py --port 8791 &
sleep 3

# 2. 接口自检
curl -s http://127.0.0.1:8791/health
curl -s http://127.0.0.1:8791/state      # 应只有 agent，无 usage
```

几何自检（无头截图 → 量像素）：

> ⚠️ **Chrome 无头有最小窗口宽度约 500px。**
> 用 <500 的宽度截图，拿到的是大视口的**左侧裁切**，角色会"看起来跑到右边"，
> 极易误判成 CSS bug。必须用 ≥500 的宽度截图。

| 断言 | 期望 |
|---|---|
| 角色显示高度 | 127~128 px（目标 120~130） |
| 左右留白 | 差值 ≤ 2 px（水平居中） |
| 距窗口底 | ≈ 20 px（= 40/400 × 200） |
| 角色宽 | ≤ 103 px（= 205/320 × 160） |

素材本身的 12 条验收标准，见外发 brief 第 12 节。
