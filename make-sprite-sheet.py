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
    """清掉与画面边缘连通的近似白/浅灰背景 → 透明。"""
    im = Image.open(path).convert("RGBA")
    a = np.array(im).astype(int)
    R, G, B = a[:, :, 0], a[:, :, 1], a[:, :, 2]
    mx = np.maximum(np.maximum(R, G), B)
    mn = np.minimum(np.minimum(R, G), B)
    bgish = (mn >= 222) & ((mx - mn) <= 12)
    fg = ~bgish
    h, w = bgish.shape
    keep = np.zeros((h, w), bool)
    for y in range(h):
        idx = np.where(fg[y])[0]
        if len(idx):
            keep[y, idx.min():idx.max()+1] = True
    for x in range(w):
        idx = np.where(keep[:, x])[0]
        if len(idx):
            keep[idx.min():idx.max()+1, x] = True
    alpha = np.where(keep, 255, 0).astype(np.uint8)
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
    ap.add_argument("--air", default="", help="腾空帧号，逗号分隔，如 4,8")
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
    air = {int(x) for x in args.air.split(",") if x.strip()}

    print()
    print("=== 3/3 贴进精灵图 ===")
    print("  %-4s %-11s %-16s %s" % ("帧", "缩放后", "贴到画布", "说明"))
    for i, im in enumerate(poses):
        idx = i + 1
        nw = max(1, int(round(im.width*K)))
        nh = max(1, int(round(im.height*K)))
        t = im.resize((nw, nh), Image.LANCZOS)
        bottom = args.ground - (args.lift if idx in air else 0)
        left = args.center - nw//2
        top = bottom - nh
        r, c = divmod(i, COLS)
        sheet.alpha_composite(t, (c*CELL_W + left, r*CELL_H + top))
        print("  #%-3d %-11s %-16s %s" % (idx, "%dx%d" % (nw, nh),
              "x=%d y=%d" % (left, top),
              "腾空（抬高 %dpx）" % args.lift if idx in air else "接地"))

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
