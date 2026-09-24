#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""run 第 2 版：脚锁定版（foot lock）—— 用 1 骨段 IK 重建 12 帧。

为什么这么做（见 docs/为什么跑步这么难-根因诊断.md）：
  第 1 版（外部定稿）姿势、尺寸、稳定性都对，但**支撑期脚不打滑这条不满足** ——
  实测脚每帧只后移 4.0px，而按窗口速度应该后移 15.0px（达成 27%）。
  脚不向后走 = 踩跑步机滑行 = "死板"的第一真凶。

关键简化：**这条裙子遮住大腿**，所以可见的腿只有「小腿 + 脚」。
  → 不用做两骨段（大腿+小腿），把可见腿当成**一根骨段**就够了：
    绕脚底旋转 + 沿轴伸缩，顶部藏在裙摆下面看不见。
  → 于是「脚在地面上的位置」可以由我**直接指定**，脚锁定变成构造保证，
    而不是"祈祷生成模型画对"。

给脚指定的轨迹（相对身体中线 x=160，18fps，每帧 15px）：
  支撑期（4 帧）：x = 190 → 175 → 160 → 145（每帧 −15px 匀速后移 = 脚锁定）
  摆动期（8 帧）：从 145 平滑回到 190，中段抬起（弧线）
  左腿支撑在 1~4 帧、右腿支撑在 7~10 帧；5/6 与 11/12 帧两脚都在摆动 → 腾空

用法：
    python3 rebuild-run-footlock.py --src assets/pet/run.png --out /tmp/run-v2.png
"""
import argparse
import os
import sys

import numpy as np
from PIL import Image

FW, FH, COLS, ROWS = 320, 400, 6, 2
CELL_W, CELL_H = 320, 400
HEM = 306          # 裙摆下沿（身体/腿 分界线）
HIP = (160, 300)   # 髋关节（旋转/伸缩的锚点，藏在裙摆下）
GROUND = 358       # 接地线
FRONT_X = 190      # 前脚位置
BACK_X = 145       # 后脚位置
STEP_PX = 15       # 每帧后移量 = stridePxPerCycle / 帧数 = 180/12
LIFT = 22          # 摆动期抬脚高度


def cell_of(sheet, i):
    r, c = divmod(i, COLS)
    return sheet.crop((c*FW, r*FH, (c+1)*FW, (r+1)*FH))


def tight_box(mask):
    ys = np.where(mask.any(axis=1))[0]
    xs = np.where(mask.any(axis=0))[0]
    return xs.min(), ys.min(), xs.max(), ys.max()


def foot_schedule(frame_idx):
    """返回两只脚在第 i 帧（0-based）的目标位置 [(x, y), (x, y)]（左、右）。

    左腿支撑 1~4 帧、右腿支撑 7~10 帧；其余帧在摆动。
    支撑期：每帧 −STEP_PX（脚锁定）；摆动期：从后回到前 + 抬脚弧线。
    """
    out = []
    for side in (0, 1):                     # 0=左（前 6 帧的主角）, 1=右
        stance_start = 0 if side == 0 else 6        # 4 帧支撑
        cycle = (frame_idx - stance_start) % 12
        if 0 <= cycle < 4:
            x = FRONT_X - cycle * STEP_PX           # 190 → 175 → 160 → 145
            y = GROUND                               # 踩在地上
        else:
            t = (cycle - 4) / 8.0                    # 摆动进度 0..1
            x = BACK_X + (FRONT_X - BACK_X) * t      # 从后往前
            # 弧线：中段抬最高；末端回落到地面
            y = GROUND - LIFT * np.sin(np.pi * t)
        out.append((x, y))
    return out


def rot_about(sprite, foot_in_sprite, target, angle, scale_along):
    """把 sprite 绕它的 foot_in_sprite 旋转 angle、沿轴伸缩 scale_along，
    再把这个点对齐到 target。返回与画布同尺寸的图层。

    PIL 的 AFFINE 需要的是「输出坐标 → 输入坐标」的逆映射：
        x_in = a*x_out + b*y_out + c
        y_in = d*x_out + e*y_out + f
    """
    th = -angle                      # 逆变换用 −θ
    ca, sa = np.cos(th), np.sin(th)
    a, b = ca, -sa
    d, e = sa, ca
    # 沿腿轴方向伸缩（轴方向 = 从脚指向髋；简化成对整体做等比长度修正）
    a, b, d, e = a*scale_along, b*scale_along, d*scale_along, e*scale_along
    c = foot_in_sprite[0] - (a*target[0] + b*target[1])
    f = foot_in_sprite[1] - (d*target[0] + e*target[1])
    return sprite.transform((CELL_W, CELL_H), Image.AFFINE, (a, b, c, d, e, f),
                            resample=Image.BICUBIC)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", default="assets/pet/run.png")
    ap.add_argument("--out", default="/tmp/workchk/run-v2.png")
    ap.add_argument("--debug-dir", default="/tmp/workchk/v2dbg")
    args = ap.parse_args()

    sheet = Image.open(args.src).convert("RGBA")
    os.makedirs(args.debug_dir, exist_ok=True)

    # ---- 1. 身体：取第 1 帧，切掉裙摆以下，并把裙摆向下延长一点遮住腿根 ----
    f1 = cell_of(sheet, 0)
    ba = np.array(f1)
    ba[HEM:, :, 3] = 0
    # 裙摆下沿向下"拉长" 12px（复制最后几行），保证腿旋转到任何角度都不露缝
    strip = ba[HEM-6:HEM, :, :].copy()
    body_arr = ba.copy()
    for k in range(12):
        src_row = strip[k % 6]
        if HEM + k < CELL_H:
            body_arr[HEM + k] = src_row
    body = Image.fromarray(body_arr)
    body.save(os.path.join(args.debug_dir, "body.png"))

    # ---- 2. 一条标准腿精灵：取第 1 帧前腿（分界线以下的连通块）----
    A = np.array(f1.split()[-1]) > 128
    legmask = A.copy()
    legmask[:HEM] = False
    # 前腿 = 最靠右的鞋块所在的那一连通块 → 用列的连通段近似
    cols_any = legmask.any(axis=0)
    runs, st = [], None
    for k, v in enumerate(cols_any):
        if v and st is None:
            st = k
        elif not v and st is not None:
            runs.append((st, k-1))
            st = None
    if st is not None:
        runs.append((st, len(cols_any)-1))
    x0, x1 = runs[-1]                                  # 最右的一段 = 前腿
    sub = legmask[:, x0:x1+1]
    x0b, y0b, x1b, y1b = tight_box(sub)
    leg = f1.crop((x0 + x0b, y0b, x0 + x1b + 1, y1b + 1))
    leg.save(os.path.join(args.debug_dir, "leg_canon.png"))
    # 脚底参考点：精灵底部的中点（= 鞋底中心）
    foot_local = (leg.width / 2.0, leg.height - 1.0)
    leg_len = leg.height
    print("身体已提取（裙摆下沿 y=%d，下延 12px）" % HEM)
    print("标准腿精灵：%dx%d，脚底参考点 %s，腿长参考 %d px"
          % (leg.width, leg.height, foot_local, leg_len))

    # ---- 3. 逐帧合成 ----
    out = Image.new("RGBA", (CELL_W*COLS, CELL_H*ROWS), (0, 0, 0, 0))
    print()
    print("%-4s %-30s %s" % ("帧", "两脚目标位置(x,y)", "腿长/角度"))
    for i in range(12):
        r, c = divmod(i, COLS)
        ox, oy = c*CELL_W, r*CELL_H
        layer = Image.new("RGBA", (CELL_W, CELL_H), (0, 0, 0, 0))
        info = []
        for side, (tx, ty) in enumerate(foot_schedule(i)):
            dx = HIP[0] - tx
            dy = HIP[1] - ty
            dist = np.hypot(dx, dy)
            # 腿轴方向：从脚指向髋。精灵本身是"竖直向下"画的 → 需要的旋转角
            angle = np.arctan2(dx, -dy)                # 0 = 竖直
            scale = dist / float(leg_len)
            lay = rot_about(leg, foot_local, (tx, ty), angle, scale)
            layer.alpha_composite(lay)
            info.append("(%.0f,%.0f) 长%.0f 角%.0f°" % (tx, ty, dist, np.degrees(angle)))
        # 先腿后身 → 裙摆自然盖住腿根
        out.alpha_composite(layer, (ox, oy))
        out.alpha_composite(body, (ox, oy))
        print("%-4d %-30s %s" % (i+1, " | ".join(info), ""))

    out.save(args.out)
    print()
    print("已写出:", args.out, out.size)
    return 0


if __name__ == "__main__":
    sys.exit(main())
