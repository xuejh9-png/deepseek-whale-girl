#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把一组「单姿势图」合成成桌宠可用的精灵图（Sprite Sheet）。

这条链路的最后一步，也是**唯一能保证"多帧尺寸一致"**的一步 ——
生成模型保证不了（上一版 run 就是因为没人统一尺寸，被横向压窄 24%、
循环内"胖瘦脉动"两次）。所以尺寸一致性**由合成保证，不靠生成碰运气**。

用法：
    python3 make-sprite-sheet.py --src <姿势图目录> --out assets/pet/run.png \
        --frames 8 --air 4,8

约定：
  · 目录里的 png **按文件名排序 = 播放顺序**（生成时就按 01/02/… 命名）
  · 背景自动抠掉：清掉"与画面边缘连通"的近似白/浅灰。
    为什么不用简单阈值：角色本人有大片白色（裙子/头饰），阈值化会一起吃掉。
    按"每行最左/最右的非背景像素"只在两侧清，角色内部的白色天然保留。
  · **全局单一缩放**（--char-width）：所有帧用同一个系数。
    ⚠️ 不要逐帧拉齐高度 —— 那会把"压低帧本来就矮、腾空帧该收腿"抹掉，
    等于把正确的动画改坏。
  · 接地帧脚底对齐 --ground；腾空帧（--air）整体抬高 --lift
  · 水平按轮廓包围盒居中到 --center

退出码：0 = 成功
"""
import argparse
import glob
import os
import sys

import numpy as np
from PIL import Image

CELL_W, CELL_H, COLS = 320, 400, 6


def keyout(path):
    """清掉与画面边缘连通的近似白/浅灰背景 → 透明。

    ⚠️ 2026-09-23 返工：上一版是"每行取最左/最右的非背景像素之间全保留"，
    结果**每帧右边/下面都残留一块背景矩形**（用户截图里那个"白色方块"）。
    根因：角色有分开的部位（飘起的头发、抬起的腿），那一行的最左最右之间
    夹着大片背景，被整块保下来了。

    正确做法是**从画面边缘往内做连通域灌注**：
      · 她本人的白（裙子/围裙/头饰/鞋子）和背景同色，**阈值化会一起吃掉**
      · 但她的白被轮廓包着，**灌注进不去** —— "与边缘连通"这一个条件就分开了
    """
    im = Image.open(path).convert("RGBA")
    a = np.array(im).astype(int)
    mn = a[:, :, :3].min(axis=2)
    mx = a[:, :, :3].max(axis=2)
    bright = (mn >= 235) & ((mx - mn) <= 10)

    seed = np.zeros_like(bright)
    seed[0, :] = bright[0, :]
    seed[-1, :] = bright[-1, :]
    seed[:, 0] = bright[:, 0]
    seed[:, -1] = bright[:, -1]
    while True:
        g = seed.copy()
        g[1:, :] |= seed[:-1, :]
        g[:-1, :] |= seed[1:, :]
        g[:, 1:] |= seed[:, :-1]
        g[:, :-1] |= seed[:, 1:]
        g &= bright
        if np.array_equal(g, seed):
            break
        seed = g

    alpha = np.where(seed, 0, 255).astype(np.uint8)
    return Image.fromarray(np.dstack([a[:, :, :3].astype(np.uint8), alpha]), "RGBA")


def tight(im):
    a = np.array(im.split()[-1]) > 128
    ys = np.where(a.any(axis=1))[0]
    xs = np.where(a.any(axis=0))[0]
    return im.crop((xs.min(), ys.min(), xs.max()+1, ys.max()+1))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", required=True, help="姿势图目录")
    ap.add_argument("--out", required=True, help="输出的精灵图路径")
    ap.add_argument("--frames", type=int, help="帧数（默认=目录里的 png 数量）")
    ap.add_argument("--air", default="",
                    help="腾空帧号，如 5,11；也可以逐帧指定抬高量：5:40,6:20,11:40,12:20")
    ap.add_argument("--lift", type=int, default=40, help="腾空帧整体抬高多少 px")
    ap.add_argument("--ground", type=int, default=358, help="接地线 y（脚底落在这里）")
    ap.add_argument("--center", type=int, default=160, help="水平中心 x")
    ap.add_argument("--char-width", type=int, default=202,
                    help="角色目标宽度（最宽的一帧缩到这个宽度，其余同系数）")
    ap.add_argument("--no-key", action="store_true", help="跳过抠图（源图已是透明）")
    args = ap.parse_args()

    srcs = sorted(glob.glob(os.path.join(args.src, "*.png")))
    if args.no_key:
        srcs = sorted(glob.glob(os.path.join(args.src, "*.png")))
    if not srcs:
        print("FAIL: %s 里没有 png" % args.src)
        return 1
    n = args.frames or len(srcs)
    if len(srcs) < n:
        print("FAIL: 需要 %d 帧，目录里只有 %d 张" % (n, len(srcs)))
        return 1
    srcs = srcs[:n]

    outdir = os.path.dirname(os.path.abspath(args.out))
    tmpkey = os.path.join(outdir, ".key")
    os.makedirs(tmpkey, exist_ok=True)

    print("=== 1/3 抠图 ===")
    poses = []
    for i, p in enumerate(srcs):
        im = tight(keyout(p) if not args.no_key else Image.open(p).convert("RGBA"))
        poses.append(im)
        print("  #%d %s  %dx%d" % (i+1, os.path.basename(p), im.width, im.height))

    print()
    print("=== 2/3 统一缩放（K 由最宽的一帧决定）===")
    maxw = max(p.width for p in poses)
    K = args.char_width / float(maxw)
    print("  最宽帧 %dpx → %dpx，K = %.5f" % (maxw, args.char_width, K))

    rows = (n + COLS - 1) // COLS
    sheet = Image.new("RGBA", (CELL_W*COLS, CELL_H*rows), (0, 0, 0, 0))
    # 腾空帧 → 抬高量。支持 "5:40,6:20" 这种逐帧指定：
    # 一次腾空常常占两帧（顶点 + 下落），两帧用同一个抬高量会让落地前"悬住"，
    # 下落那帧应该只抬一半左右，形成下落弧线。
    air = {}
    for tok in args.air.split(","):
        tok = tok.strip()
        if not tok:
            continue
        if ":" in tok:
            k, v = tok.split(":")
            air[int(k)] = int(v)
        else:
            air[int(tok)] = args.lift

    print()
    print("=== 3/3 贴进精灵图 ===")
    print("  %-4s %-11s %-16s %s" % ("帧", "缩放后", "贴到画布", "说明"))
    for i, im in enumerate(poses):
        idx = i + 1
        nw = max(1, int(round(im.width*K)))
        nh = max(1, int(round(im.height*K)))
        t = im.resize((nw, nh), Image.LANCZOS)
        bottom = args.ground - air.get(idx, 0)
        left = args.center - nw//2
        top = bottom - nh
        r, c = divmod(i, COLS)
        sheet.alpha_composite(t, (c*CELL_W + left, r*CELL_H + top))
        print("  #%-3d %-11s %-16s %s" % (idx, "%dx%d" % (nw, nh),
              "x=%d y=%d" % (left, top),
              "腾空（抬高 %dpx）" % air[idx] if idx in air else "接地"))

    sheet.save(args.out)
    print()
    print("已写出: %s  %dx%d（%d 列 × %d 行，%d 帧）"
          % (args.out, sheet.width, sheet.height, COLS, rows, n))

    # 立刻复核一遍（合成后重新量，防止"以为对上了其实没有"）
    A = np.array(sheet.split()[-1]) > 128
    print()
    print("=== 合成后复核 ===")
    print("  %-4s %6s %6s %8s %8s" % ("帧", "宽", "高", "脚底y", "中心x"))
    for i in range(n):
        r, c = divmod(i, COLS)
        m = A[r*CELL_H:(r+1)*CELL_H, c*CELL_W:(c+1)*CELL_W]
        ys = np.where(m.any(axis=1))[0]
        xs = np.where(m.any(axis=0))[0]
        print("  #%-3d %6d %6d %8d %8.1f"
              % (i+1, xs.max()-xs.min()+1, ys.max()-ys.min()+1,
                 ys.max(), (xs.min()+xs.max())/2.0))
    print()
    print("下一步：python3 verify-assets.py 验收")
    return 0


if __name__ == "__main__":
    sys.exit(main())
