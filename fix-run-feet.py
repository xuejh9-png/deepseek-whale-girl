#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把鞋的旋转角约束到 0~20°（路 B）—— 消除"皮影戏"式的脚踝。

为什么需要这一步（2026-09-24 用户反馈）：
  外部交付是"一张平面鞋图绕踝关节任意旋转"，实测鞋底倾角最大到 83° ——
  人脚踝折不到那个角度。用户的原话是"根本不是人，像皮影戏"。
  真动画师会画好几双鞋按帧换图；我们不改出图流程，改成**把越界的旋转回正**。

做法（逐帧、逐只鞋）：
  1. 在腿部区域找鞋的像素块（深蓝），按列分段并合并
  2. 对每只鞋算主轴倾角 θ（PCA 长轴）
  3. 目标角 = clamp(θ, 0°, 20°)
  4. 绕**鞋底接触点**（该鞋最低像素所在处）把鞋转回目标角
     —— 绕接触点转，脚就永远踩在地面上，不会因为回正而飘起来
  5. 清掉原来的鞋像素，贴回回正后的鞋

用法：
    python3 fix-run-feet.py --src assets/pet/run.png --out /tmp/run-fixed.png
"""
import argparse
import os
import sys

import numpy as np
from PIL import Image

FW, FH, COLS, ROWS = 320, 400, 6, 2
CELL_W, CELL_H = 320, 400
LEG_TOP = 300          # 只看裙摆以下（避免把深蓝围裙当鞋）
TILT_MIN = 0.0         # 允许的最小倾角
TILT_MAX = 20.0        # 允许的最大倾角（用户指定 0~20°）


def shoe_mask(cell):
    a = np.array(cell)
    al = a[:, :, 3] > 200
    R, G, B = a[:, :, 0].astype(int), a[:, :, 1].astype(int), a[:, :, 2].astype(int)
    m = al & (R < 115) & (G < 125) & (B > 85) & (B > R + 10)
    m[:LEG_TOP] = False
    return m


def blobs_of(mask):
    cols = mask.any(axis=0)
    segs, run, st = [], False, 0
    for k in range(len(cols)):
        v = cols[k]
        if v and not run:
            run, st = True, k
        elif not v and run:
            run = False
            if k - st >= 5:
                segs.append((st, k))
    if run and len(cols) - st >= 5:
        segs.append((st, len(cols)))
    merged = []
    for s in segs:
        if merged and s[0] - merged[-1][1] <= 8:
            merged[-1] = (merged[-1][0], s[1])
        else:
            merged.append(list(s))
    out = []
    for x0, x1 in merged:
        sub = mask[:, x0:x1]
        ys = np.where(sub.any(axis=1))[0]
        if len(ys) == 0:
            continue
        out.append((x0, x1, int(ys.min()), int(ys.max())))
    return out


def tilt_of(mask, x0, x1):
    sub = mask[:, x0:x1]
    ys, xs = np.where(sub)
    if len(xs) < 30:
        return None
    X = np.stack([xs - xs.mean(), ys - ys.mean()])
    cov = X @ X.T / len(xs)
    w, v = np.linalg.eigh(cov)
    axis = v[:, int(np.argmax(w))]
    ang = np.degrees(np.arctan2(axis[1], axis[0]))
    if ang > 90:
        ang -= 180
    if ang < -90:
        ang += 180
    return ang


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", default="assets/pet/run.png")
    ap.add_argument("--out", default="/tmp/workchk/run-feet-fixed.png")
    args = ap.parse_args()

    sheet = Image.open(args.src).convert("RGBA")
    out = Image.new("RGBA", sheet.size, (0, 0, 0, 0))
    print("%-4s %-34s %s" % ("帧", "回正前 → 后（每只鞋）", "旋转量"))
    for i in range(12):
        r, c = divmod(i, COLS)
        cell = out.crop((c*CELL_W, r*CELL_H, (c+1)*CELL_W, (r+1)*CELL_H))
        cell = sheet.crop((c*CELL_W, r*CELL_H, (c+1)*CELL_W, (r+1)*CELL_H)).copy()
        m = shoe_mask(cell)
        before, after, deltas = [], [], []
        for x0, x1, y0, y1 in blobs_of(m):
            ang = tilt_of(m, x0, x1)
            if ang is None:
                continue
            target = min(max(ang, TILT_MIN), TILT_MAX)
            before.append(ang)
            after.append(target)
            d = target - ang
            deltas.append(d)
            if abs(d) < 0.5:
                continue
            pad = 6
            bx0, bx1 = max(0, x0 - pad), min(CELL_W, x1 + pad)
            by0, by1 = max(0, y0 - pad), min(CELL_H, y1 + pad)
            sub_m = m[by0:by1, bx0:bx1]
            sub = cell.crop((bx0, by0, bx1, by1))
            # 接触点 = 该鞋最低像素所在处
            cy, cx = np.where(sub_m)
            k = int(np.argmax(cy))
            pivot = (bx0 + cx[k], by0 + cy[k])
            # 清掉原来的鞋像素
            arr = np.array(cell)
            reg = arr[by0:by1, bx0:bx1]
            reg[sub_m, 3] = 0
            arr[by0:by1, bx0:bx1] = reg
            cell = Image.fromarray(arr)
            # 绕接触点旋转 —— ⚠️ 必须把支点放进仿射矩阵里。
            # 一开始我用了纯旋转矩阵（绕精灵左上角），再把精灵按包围盒贴回去，
            # 结果鞋被转得更歪（倾角 68° → 89°）。支点必须是鞋底接触点。
            # PIL 的 AFFINE 要的是"输出→输入"的映射，输入局部坐标里支点 = (cx, cy)：
            #   ix = ca*(ox-cx) + sa*(oy-cy) + cx
            #   iy = -sa*(ox-cx) + ca*(oy-cy) + cy
            d_rad = np.deg2rad(d)
            ca, sa = np.cos(d_rad), np.sin(d_rad)
            pcx, pcy = float(cx[k]), float(cy[k])
            A = (ca, sa, pcx - ca*pcx - sa*pcy,
                 -sa, ca, pcy + sa*pcx - ca*pcy)
            spr = sub.transform(sub.size, Image.AFFINE, A, resample=Image.BICUBIC)
            cell.alpha_composite(spr, (bx0, by0))
        out.alpha_composite(cell, (c*CELL_W, r*CELL_H))
        print("#%-3d %-34s %s"
              % (i+1,
                 " ".join("%.0f°→%.0f°" % (b, a) for b, a in zip(before, after)),
                 " ".join("%+.0f°" % d for d in deltas)))

    out.save(args.out)
    print()
    print("已写出:", args.out, out.size)
    return 0


if __name__ == "__main__":
    sys.exit(main())
