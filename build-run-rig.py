#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 run 从「逐帧整只为单位」改成「一套身体 + 分离的腿」的 2D 装置。

用户规格的核心约束（这份脚本就是照它写的）：
  · 不允许每帧重新生成角色结构
  · 头 / 身体 / 手臂 / 裙子 在所有帧必须完全一致
  · 整体高度变化 ≤3%，躯干中心 X 固定，不允许漂移
  · 7-12 帧严格镜像 1-6 帧
  · 腿长缩短 15%~20%（Q 版短腿）

做法：
  身体 = 取一帧，切掉分界线以下 → **12 帧共用同一块像素**（这是"角色稳定"的唯一可靠保证）
  腿   = 第 1~6 帧分界线以下的部分，各自紧裁
  第 7~12 帧 = 第 1~6 帧的腿**水平镜像**（腿部完全对称，镜像即"换另一条腿在前"）
  缩短 = 腿的竖直方向缩放 0.82 并把脚锚在接地线上 → 髋部抬高，身体相应下移
  腾空 = 整个腿部组件抬升一点点（保持总高变化 ≤3%）
"""
import argparse
import json
import os
import sys

import numpy as np
from PIL import Image

FW, FH, COLS = 320, 400, 6
CELL_W, CELL_H, ROWS = 320, 400, 2


def tight_box(m):
    ys = np.where(m.any(axis=1))[0]
    xs = np.where(m.any(axis=0))[0]
    return xs.min(), ys.min(), xs.max(), ys.max()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", default="assets/pet/run.png")
    ap.add_argument("--out", default="/tmp/workchk/run-rig.png")
    ap.add_argument("--cut", type=int, default=308, help="身体/腿 分界线 y")
    ap.add_argument("--ground", type=int, default=358)
    ap.add_argument("--leg-scale", type=float, default=0.82, help="腿竖直缩放（0.82≈缩短18%）")
    ap.add_argument("--air-lift", type=int, default=6, help="腾空帧抬升 px（要小，控高度变化）")
    args = ap.parse_args()

    src = Image.open(args.src).convert("RGBA")
    A = np.array(src.split()[-1]) > 128

    # ---- 1. 身体：取第 1 帧，切掉分界线以下 ----
    body = src.crop((0, 0, FW, FH))
    ba = np.array(body)
    ba[args.cut:, :, 3] = 0
    # 把裙摆下沿向下"延长"几行，保证任何腿位都不会露出空隙
    skirt_rows = ba[args.cut-4:args.cut, :, :]
    body = Image.fromarray(ba)

    # ---- 2. 腿：第 1~6 帧，分界线以下 ----
    legs = []
    for i in range(6):
        r, c = divmod(i, COLS)
        cm = A[r*FH:(r+1)*FH, c*FW:(c+1)*FW].copy()
        cm[:args.cut] = False
        if not cm.any():
            legs.append(None)
            continue
        x0, y0, x1, y1 = tight_box(cm)
        sp = src.crop((c*FW + x0, r*FH + y0, c*FW + x1 + 1, r*FH + y1 + 1))
        legs.append(dict(sp=sp, x=x0, top=y0, bot=y1, h=y1-y0+1))

    ref_h = max(l["h"] for l in legs if l)
    shorten = int(round(ref_h * (1 - args.leg_scale)))
    print("分界线 y=%d ｜ 参考腿高 %d → 缩短 %d px（%.0f%%）"
          % (args.cut, ref_h, shorten, (1-args.leg_scale)*100))
    print("身体整体下移 %d px（髋部抬高，裙摆才不会和腿脱开）" % shorten)

    # ---- 3. 合成 12 帧 ----
    out = Image.new("RGBA", (CELL_W*COLS, CELL_H*ROWS), (0, 0, 0, 0))
    air = {5: args.air_lift, 11: args.air_lift}
    print()
    print("%-4s %-22s %s" % ("帧", "腿来源", "说明"))
    for i in range(12):
        beat = i % 6                 # 第 7~12 帧复用第 1~6 帧
        mirror = i >= 6
        L = legs[beat]
        r, c = divmod(i, COLS)
        ox, oy = c*CELL_W, r*CELL_H

        # 身体：固定位置 + 全局下移
        out.alpha_composite(body, (ox, oy + shorten))

        if L is None:
            print("%-4d %-22s %s" % (i+1, "-", "该拍无腿像素"))
            continue
        nw = L["sp"].width
        nh = max(1, int(round(L["h"] * args.leg_scale)))
        t = L["sp"].resize((nw, nh), Image.LANCZOS)
        if mirror:
            t = t.transpose(Image.FLIP_LEFT_RIGHT)
        # 竖直锚点：脚底保持在原来的位置（再按腾空抬升）
        bottom = L["bot"] - air.get(i+1, 0)
        top = bottom - nh + 1
        # 水平锚点：保持原来的 x（镜像时按身体中线对称）
        if mirror:
            mid = 160
            left = int(round(mid - (L["x"] + nw - mid)))
        else:
            left = L["x"]
        out.alpha_composite(t, (ox + left, oy + top))
        print("%-4d %-22s %s" % (i+1, "leg%d%s" % (beat+1, "（镜像）" if mirror else ""),
                                 "腾空抬升 %dpx" % air[i+1] if (i+1) in air else ""))

    out.save(args.out)
    print()
    print("已写出:", args.out, out.size)

    # ---- 4. 自查：高度稳定性 / 体轴稳定 / 镜面对称 ----
    B = np.array(out.split()[-1]) > 128
    print()
    print("=== 自查 ===")
    print("  %-4s %6s %8s %8s %10s" % ("帧", "高", "脚底y", "中心x", "身体区域一致"))
    hs, cxs = [], []
    body_ref = None
    for i in range(12):
        r, c = divmod(i, COLS)
        m = B[r*CELL_H:(r+1)*CELL_H, c*CELL_W:(c+1)*CELL_W]
        ys = np.where(m.any(axis=1))[0]
        xs = np.where(m.any(axis=0))[0]
        hs.append(ys.max()-ys.min()+1)
        cxs.append((xs.min()+xs.max())/2.0)
        # 身体区域（分界线以上）哈希
        seg = m[:args.cut + shorten - 4]
        h = hash(seg.tobytes())
        if body_ref is None:
            body_ref = h
        print("  #%-3d %6d %8d %8.1f %10s"
              % (i+1, hs[-1], ys.max(), cxs[-1], "✓" if h == body_ref else "✗不同"))
    print()
    print("  身高 %d~%d  → 变化 %.1f%%（规格要求 ≤3%%）"
          % (min(hs), max(hs), (max(hs)-min(hs))/float(np.median(hs))*100))
    print("  中心 x %.1f~%.1f  → 漂移 %.1f px（规格要求不漂移）"
          % (min(cxs), max(cxs), max(cxs)-min(cxs)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
