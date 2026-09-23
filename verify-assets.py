#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
桌宠素材验收脚本 —— 逐帧量化，不靠肉眼看图。

用法：
    python3 verify-assets.py                # 验收 assets/pet/
    python3 verify-assets.py --dir X        # 验收指定目录
    python3 verify-assets.py --quiet        # 只输出结论

退出码：0 = 通过（可能有 WARN），1 = 有 FAIL

判定思路（重要）：
  脚底位置不按"绝对 ±5"判 —— 那会因为整体偏移 1px 就误判。
  真正要防的是**帧间跳动**（切换剪辑时角色上下跳），所以：
    · FAIL：某接地帧相对「全套接地帧中位数」偏离 > 8px（真的会跳）
    · WARN：中位数本身离地面线 > 5px（系统性偏移，肉眼不可见，不阻塞）
"""
import argparse
import json
import math
import os
import sys

try:
    from PIL import Image
    import numpy as np
except ImportError:
    print("需要 Pillow 与 numpy：pip install pillow numpy")
    sys.exit(2)

# 各剪辑的离地帧（1 基）。不在表里的 = 全程接地。
AIRBORNE = {
    'run':     [5, 10],                 # Airborne 相位
    'jump':    [5, 6, 7, 8, 9],         # 上升 / 顶点 / 下落
    'drag':    list(range(2, 13)),      # 起吊后全程悬空
    'success': [4, 5, 6, 7, 8],         # 跳起
}

SQUASH_CLIPS = {'jump', 'success'}      # 允许变矮（下蹲 / 落地压缩）

TOL_FEET_REL = 8        # 接地帧相对全套中位数的容差
TOL_FEET_ABS = 5        # 中位数离地面线超过这个值 → WARN
TOL_CENTER = 8
TOL_HEIGHT = 14
TOL_HEIGHT_SQUASH = 60
TOL_SEAM_RATIO = 1.5
ALPHA_TH = 128
WIDTH_MAX = 205
CHANGE_PX_TH = 8        # 单像素变化超过这个值算"这一像素变了"
MIN_CHANGED_PX = 20     # 一帧里至少这么多像素变了，才算"姿态不同"
MIN_VISIBLE_PX = 40     # 实际显示尺寸下，循环段单帧变化不得低于此值（否则肉眼读成静止）

# 位移/跑步类剪辑的「步幅」判据（2026-09-23 补，见 docs/run-返工说明.md）
LEG_TOP = 245           # 腿部起始高度（画布坐标）
# 为什么是 8：run 是 12 帧 @16fps、一循环 2 步（腾空 2 次）→ 一步 6 帧。
# 一步至少要 4 个关键姿态（触地 / 压低 / 过渡 / 腾空），两步 = 8 个。
# 步频由此正好 160 步/分，落在真人跑步的 160~180 区间 —— 帧数是对的，
# 缺的是关键姿态本身（实测只有 4 个，且两组几乎相同）。
STRIDE_MIN_POSES = 8
STRIDE_MIN_COLS = 60    # 任意两帧腿部轮廓至少要差这么多列

# 「整只被横向压窄」判据（2026-09-23 补）
# 量的是【角色上 40% 段的宽 / 角色高】—— 头部是刚体，这个比例只该随视角变、
# 不该随帧号变。实测标定：idle 自身波动仅 3%（97.9%~101.1%），
# jump/drag/success 最低 90%，所以阈值取 88% 误报风险很低。
# 用户反馈"跑的时候人物被压缩了"就是被这条抓到的：run 最低 74%。
WIDTH_RATIO_MIN = 0.88


def load(path, fw, fh, cols, n):
    im = Image.open(path).convert("RGBA")
    a = np.array(im.getchannel("A"))
    rgb = np.array(im).astype(np.int16)
    return [((a[r*fh:(r+1)*fh, c*fw:(c+1)*fw]), (rgb[r*fh:(r+1)*fh, c*fw:(c+1)*fw]))
            for r, c in (divmod(i, cols) for i in range(n))]


def d1(f1, f2):
    """两帧的【平均】像素差。

    ⚠️ 局限：会被大面积静止区域稀释。
    只有末端肢体（手指）参与的动画，平均差会接近 0 而看不出来
    —— 所以判断"姿态是否不同"必须用下面的 changed_px()。

    注：这类"每帧只动几个像素"的动作**本身就不该交付**（见 display_changes 的可见性
    下限）；但脚本仍要能把它和"完全没动"区分开，所以口径必须是像素个数。
    """
    m = (f1[0] > ALPHA_TH) | (f2[0] > ALPHA_TH)
    if m.sum() == 0:
        return 0.0
    return float(np.abs(f1[1][:, :, :3].astype(int) - f2[1][:, :, :3].astype(int))
                 .mean(axis=2)[m].mean())


def changed_px(f1, f2):
    """两帧之间【变化的像素个数】—— 比平均差敏感得多。

    实测：work 重做后手指每帧只变 26~469 个像素（占画布 0.02%~0.37%），
    平均差被静止区域稀释到 <0.5 而漏判成"没有变化"；
    换成数像素个数就不会漏。
    """
    return int((np.abs(f1[1][:, :, :3].astype(int) - f2[1][:, :, :3].astype(int))
                .max(axis=2) > CHANGE_PX_TH).sum())


def display_changes(path, fw, fh, cols, ls, le):
    """循环段在【实际显示尺寸】下的逐帧变化像素数。

    ⚠️ 为什么必须单独算一遍（2026-09-23 补）：
    上面的 changed_px() 数的是 320×400 **原画布**上的像素。work 返工版每帧
    变 26~73px，按 MIN_CHANGED_PX=20 判定"8/8 姿态全过" —— 验收通过。
    但运行时角色是 0.5 倍显示（160×200），同样的动作缩掉 3/4 面积后
    只剩 8~14 个屏幕像素，低于人眼可察觉阈值，用户看到的就是"完全不动"。

    姿态判定回答的是「有没有变」，这里回答的是「变得看不看得见」。
    前者过不等于后者过 —— 这就是 work 那种"做废了的动作"能溜过验收的原因。
    """
    im = Image.open(path).convert("RGBA")
    half = im.resize((im.width // 2, im.height // 2), Image.LANCZOS)
    fw2, fh2 = fw // 2, fh // 2

    def cell(i):
        r, c = divmod(i, cols)
        return np.array(half.crop((c * fw2, r * fh2, (c + 1) * fw2, (r + 1) * fh2))
                        .convert("RGB")).astype(int)

    out = []
    for i in range(ls, le):
        a, b = cell(i - 1), cell(i)
        out.append(int((np.abs(a - b).max(axis=2) > CHANGE_PX_TH).sum()))
    return out


def stride_profile(path, fw, fh, cols, n, leg_top=LEG_TOP):
    """量「腿有没有真的前后摆」—— 位移/跑步类剪辑的核心判据。

    ⚠️ 为什么必须单独量一遍（2026-09-23 补）：
    用户反馈 run「看起来像平移过去，不生动」，但脚本原有判据**全绿** ——
    因为"整帧有没有变"是 12/12（头发、披风一直在动），
    而真正决定"像不像在跑"的是**腿部轮廓的水平走向**有没有变。

    实测 run：12 帧里只有 4 个水平姿态、任意两帧最多差 22 列（画布宽 320）。
    腿部像素总量确实每帧在变（3000~7000px），但那些变化全在**竖直方向**
    （跟着身体上下弹跳），前后交替几乎为零。
    → 合起来就是"站着不动 + 身体弹 2 次"，运行时配上窗口横移 = "颠着平移"。

    返回 (姿态数, 任意两帧的最大列差)。
    """
    im = Image.open(path).convert("RGBA")
    A = np.array(im.split()[-1]).astype(int)
    sigs = []
    for i in range(n):
        r, c = divmod(i, cols)
        m = A[r*fh:(r+1)*fh, c*fw:(c+1)*fw] > ALPHA_TH
        band = np.zeros(m.shape, bool)
        band[leg_top:] = True
        band &= m
        sigs.append(band.any(axis=0))       # 每列有没有腿像素 = 水平轮廓

    used, poses = set(), 0
    for i in range(n):
        if i in used:
            continue
        poses += 1
        used.add(i)
        for j in range(i + 1, n):
            if j not in used and int((sigs[i] != sigs[j]).sum()) < 8:
                used.add(j)
    maxcols = 0
    for i in range(n):
        for j in range(i + 1, n):
            maxcols = max(maxcols, int((sigs[i] != sigs[j]).sum()))
    return poses, maxcols


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default=os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                                  "assets", "pet"))
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    root = args.dir
    mp = os.path.join(root, "manifest.json")
    if not os.path.exists(mp):
        print(f"FAIL: 找不到 {mp}")
        return 1
    man = json.load(open(mp, encoding="utf-8"))
    ss = man["spriteSheet"]
    FW, FH, COLS = ss["frameWidth"], ss["frameHeight"], ss["columns"]
    GX, GY = ss["anchorX"] * FW, ss["anchorY"] * FH
    CH = ss.get("characterHeight", 256)

    fails, warns, report = [], [], []

    # ---------- 第一遍：装载 + 量测 ----------
    data = {}
    for clip, m in man["clips"].items():
        p = os.path.join(root, m["file"])
        if not os.path.exists(p):
            fails.append(f"{clip}: 缺文件 {m['file']}")
            continue
        n = m["frameCount"]
        im = Image.open(p)
        if im.size != (COLS * FW, m["rows"] * FH):
            fails.append(f"{clip}: 尺寸 {im.size} ≠ {COLS*FW}×{m['rows']*FH}")
        if m["rows"] != math.ceil(n / COLS):
            fails.append(f"{clip}: rows={m['rows']} ≠ ceil({n}/{COLS})")
        if im.mode != "RGBA":
            fails.append(f"{clip}: 模式 {im.mode}，应为 RGBA")

        frames = load(p, FW, FH, COLS, n)
        arr = np.array(im.convert("RGBA"))
        air = set(AIRBORNE.get(clip, []))
        metrics = []
        for i, fr in enumerate(frames):
            ys, xs = np.where(fr[0] > ALPHA_TH)
            if len(xs) == 0:
                fails.append(f"{clip}#{i+1}: 空帧")
                metrics.append(None)
                continue
            x0, x1, y0, y1 = xs.min(), xs.max(), ys.min(), ys.max()
            h = int(y1 - y0 + 1)
            # 头段（角色上 40%）宽度 / 角色高 —— 查"整只被横向压窄"
            band = fr[0][y0:y0 + max(1, int(h * 0.40))]
            bxs = np.where(band.any(axis=0))[0]
            hw = int(bxs.max() - bxs.min() + 1) if len(bxs) else 0
            metrics.append(dict(
                feet=int(y1), cx=(x0 + x1) / 2, h=int(y1 - y0 + 1), w=int(x1 - x0 + 1),
                hratio=(hw / float(h) if h else 0.0),
                air=(i + 1) in air, idx=i + 1))
        # 四角 / 背景
        for i in range(n):
            r, c = divmod(i, COLS)
            cell = arr[r*FH:(r+1)*FH, c*FW:(c+1)*FW, 3]
            if max(cell[:8, :8].max(), cell[:8, -8:].max(),
                   cell[-8:, :8].max(), cell[-8:, -8:].max()) > 0:
                fails.append(f"{clip}#{i+1}: 四角不透明")
                break
        bg = arr[arr[:, :, 3] == 0]
        nbg = len(np.unique(bg[:, :3], axis=0)) if len(bg) else 0
        if nbg > 1:
            fails.append(f"{clip}: 透明区有 {nbg} 种颜色 → 背景被画了东西（棋盘格？）")
        data[clip] = dict(meta=m, frames=frames, metrics=metrics)

    # ---------- 第二遍：相对一致性 ----------
    all_ground = [mm["feet"] for c in data.values() for mm in c["metrics"]
                  if mm and not mm["air"]]
    base = int(np.median(all_ground)) if all_ground else int(GY)

    # 横向缩放的基准取 idle —— idle 是"标准姿态"，其它剪辑都该跟她同一比例
    hr_base = None
    if "idle" in data:
        rs = [mm["hratio"] for mm in data["idle"]["metrics"] if mm and mm["hratio"]]
        if rs:
            hr_base = float(np.median(rs))

    if abs(base - GY) > TOL_FEET_ABS:
        warns.append(f"全套接地帧脚底中位数 {base}，离地面线 {GY:.0f} 偏 {base-GY:+.0f}px"
                     f"（约 {(base-GY)*0.5:+.1f} CSS px，肉眼不可见，不阻塞）")

    for clip, c in data.items():
        m, frames, metrics = c["meta"], c["frames"], c["metrics"]
        n = m["frameCount"]
        squash = clip in SQUASH_CLIPS
        hs, ws, airs = [], [], []
        for mm in metrics:
            if not mm:
                continue
            hs.append(mm["h"]); ws.append(mm["w"])
            if mm["feet"] > GY + 1:
                fails.append(f"{clip}#{mm['idx']}: 穿地（脚底 {mm['feet']} > 地面线 {GY:.0f}）")
            if mm["air"]:
                airs.append(f"#{mm['idx']}={mm['feet']}")
                if mm["feet"] >= GY - 2:
                    fails.append(f"{clip}#{mm['idx']}: 应离地却贴地（脚底 {mm['feet']}）")
            else:
                if abs(mm["feet"] - base) > TOL_FEET_REL:
                    fails.append(f"{clip}#{mm['idx']}: 接地帧脚底 {mm['feet']} 偏离基准 {base}"
                                 f" 达 {mm['feet']-base:+d}px（会与其它帧/剪辑肉眼可见地跳）")
            if abs(mm["cx"] - GX) > TOL_CENTER:
                fails.append(f"{clip}#{mm['idx']}: 水平中心偏 {mm['cx']-GX:+.0f}")
            tol = TOL_HEIGHT_SQUASH if squash else TOL_HEIGHT
            if abs(mm["h"] - CH) > tol:
                warns.append(f"{clip}#{mm['idx']}: 角色高 {mm['h']}（期望 {CH}±{tol}）")
            if mm["w"] > WIDTH_MAX:
                fails.append(f"{clip}#{mm['idx']}: 宽 {mm['w']} > {WIDTH_MAX}")

        note = ""
        if m.get("loop"):
            ls, le = m["loopStart"], m["loopEnd"]
            inside = [d1(frames[i], frames[i+1]) for i in range(ls-1, le-1)]
            seam = d1(frames[le-1], frames[ls-1])
            mx = max(inside) if inside else 0
            if seam > mx * TOL_SEAM_RATIO and seam > 1.0:
                fails.append(f"{clip}: 循环接缝偏大 {seam:.2f}（段内最大 {mx:.2f}）")
            # 用"变化像素个数"而不是平均差 —— 否则会漏掉"只有手指在动"的动作
            chg = [changed_px(frames[i-1], frames[i]) for i in range(ls, le)]
            uniq = 1 + sum(1 for c in chg if c > MIN_CHANGED_PX)
            total = le - ls + 1
            # 「有没有变」≠「看不看得见」：再按实际显示尺寸量一遍
            dchg = display_changes(os.path.join(root, m["file"]), FW, FH, COLS, ls, le)
            note = (f"循环 {ls}-{le} 接缝 {seam:.2f} 姿态 {uniq}/{total} "
                    f"帧间变化 {min(chg)}~{max(chg)}px"
                    f" | 显示尺寸 {min(dchg)}~{max(dchg)}px")
            if uniq < total / 3:
                warns.append(f"{clip}: 循环段只有 {uniq} 个不同姿态（共 {total} 帧）→ 近似二值切换")
            if max(dchg) < MIN_VISIBLE_PX:
                fails.append(
                    f"{clip}: 循环段在实际显示尺寸（{FW//2}×{FH//2}）下每帧只变 "
                    f"{min(dchg)}~{max(dchg)}px（阈值 {MIN_VISIBLE_PX}）"
                    f" → 运行时会被人眼读成「完全不动」，等于没做动画")
        else:
            if "idle" in data:
                i1 = data["idle"]["frames"][0]
                a0, a1 = d1(frames[0], i1), d1(frames[-1], i1)
                if a0 > 1.0:
                    warns.append(f"{clip}: 首帧↔idle#1 差异 {a0:.2f}（一次性剪辑应≈0）")
                if a1 > 1.0:
                    warns.append(f"{clip}: 尾帧↔idle#1 差异 {a1:.2f}（一次性剪辑应≈0）")
                note = f"一次性 首↔idle#1 {a0:.2f} 尾↔idle#1 {a1:.2f}"

        dups = [f"{i+1}={i+2}" for i in range(n-1) if d1(frames[i], frames[i+1]) < 0.01]
        if len(dups) > max(1, n // 3):
            warns.append(f"{clip}: 相邻帧完全相同 {len(dups)} 组（{', '.join(dups[:6])}）")

        report.append(f"  {clip:8} {n:>2}帧 {m['fps']:>2}fps 高{min(hs)}-{max(hs)} 宽{max(ws)}  "
                      f"{'离地: ' + ' '.join(airs) if airs else '全程接地'}")
        if note:
            report.append(f"           {note}")

        # 位移类剪辑（manifest 里标了 flipForLeft 的）额外量步幅
        if m.get("flipForLeft"):
            poses, maxcols = stride_profile(os.path.join(root, m["file"]), FW, FH, COLS, n)
            report.append(f"           腿部位移 {poses} 个姿态 / 最大摆幅 {maxcols} 列"
                          f"（期望 ≥{STRIDE_MIN_POSES} 个、≥{STRIDE_MIN_COLS} 列）")
            if poses < STRIDE_MIN_POSES or maxcols < STRIDE_MIN_COLS:
                warns.append(
                    f"{clip}: 腿几乎没有前后摆动（{poses} 个姿态、最大摆幅 {maxcols} 列）"
                    f" → 运行时是「上下弹着平移」而不是跑，见 docs/run-返工说明.md")

        # 横向缩放：头段宽/角色高 只该随视角变，不该随帧号变
        if hr_base:
            rs = [mm["hratio"] for mm in metrics if mm and mm["hratio"]]
            if rs:
                lo = min(rs) / hr_base
                report.append(f"           角色宽度比例 最低 {lo*100:.0f}%"
                              f"（100% = 与 idle 同比例；期望 ≥{WIDTH_RATIO_MIN*100:.0f}%）")
                if lo < WIDTH_RATIO_MIN:
                    warns.append(
                        f"{clip}: 角色被横向压窄 —— 最低只有 idle 的 {lo*100:.0f}%"
                        f"（高度没变，头/上身/下身一起窄 → 是整只等比横向缩放，不是姿势）。"
                        f"运行时会看到「边跑边被捏扁」")

    # ---------- 输出 ----------
    if not args.quiet:
        print(f"验收目录: {root}")
        print(f"画布 {FW}×{FH} | {COLS} 列 | 地面线 y={GY:.0f} | 锚点 x={GX:.0f} | "
              f"剪辑 {len(man['clips'])} 个 | 接地基准 y={base}")
        print("=" * 74)
        for line in report:
            print(line)
        print("=" * 74)

    if warns:
        print(f"WARN {len(warns)} 条（不阻塞）：")
        for w in warns:
            print("  ! " + w)
    if fails:
        print(f"FAIL {len(fails)} 条：")
        for f in fails:
            print("  ✗ " + f)
        print("\n结论：不通过，需退回制作方")
        return 1
    print("结论：通过 ✓" + (f"（{len(warns)} 条 WARN）" if warns else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
