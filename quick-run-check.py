#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""只量 run 一格的快检（环境会掐长任务，所以做个小而快的版本）。
用法： python3 quick-run-check.py
"""
import json

import numpy as np
from PIL import Image

FW, FH, COLS = 320, 400, 6
GROUND = 358
LEG_TOP = 300


def shoe_blobs(cell):
    a = np.array(cell)
    al = a[:, :, 3] > 200
    R, G, B = a[:, :, 0].astype(int), a[:, :, 1].astype(int), a[:, :, 2].astype(int)
    m = al & (R < 115) & (G < 125) & (B > 85) & (B > R + 10)
    m[:LEG_TOP] = False
    cols = m.any(axis=0)
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
        sub = m[:, x0:x1]
        ys = np.where(sub.any(axis=1))[0]
        if len(ys) == 0:
            continue
        ys2, xs2 = np.where(sub)
        if len(xs2) < 30:
            out.append(((x0+x1)//2, int(ys.max()) + LEG_TOP, None))
            continue
        X = np.stack([xs2 - xs2.mean(), ys2 - ys2.mean()])
        cov = X @ X.T / len(xs2)
        w, v = np.linalg.eigh(cov)
        ax = v[:, int(np.argmax(w))]
        ang = np.degrees(np.arctan2(ax[1], ax[0]))
        if ang > 90:
            ang -= 180
        if ang < -90:
            ang += 180
        out.append(((x0+x1)//2, int(ys.max()) + LEG_TOP, abs(ang)))
    return out


def main():
    m = json.load(open("assets/pet/manifest.json", encoding="utf-8"))["clips"]["run"]
    sheet = Image.open("assets/pet/run.png").convert("RGBA")
    print("尺寸 %s ｜ manifest: %d 帧 %gfps" % (sheet.size, m["frameCount"], m["fps"]))
    A = np.array(sheet)
    al = A[:, :, 3] > 128
    tilts, hs, cxs, feet, track = [], [], [], [], []
    for i in range(12):
        r, c = divmod(i, COLS)
        cell = sheet.crop((c*FW, r*FH, (c+1)*FW, (r+1)*FH))
        msk = al[r*FH:(r+1)*FH, c*FW:(c+1)*FW]
        ys = np.where(msk.any(axis=1))[0]
        xs = np.where(msk.any(axis=0))[0]
        hs.append(ys.max() - ys.min() + 1)
        cxs.append((xs.min() + xs.max()) / 2.0)
        feet.append(ys.max())
        blobs = shoe_blobs(cell)
        ag = [b[2] for b in blobs if b[2] is not None]
        tilts.append(round(max(ag)) if ag else None)
        track.append([b[0] for b in blobs])
    print()
    print("角色高 %d~%d → 变化 %.1f%%（规格 ≤3%%）" % (min(hs), max(hs), (max(hs)-min(hs))/float(np.median(hs))*100))
    print("水平中心 %.1f~%.1f → 漂移 %.1f px（规格 0）" % (min(cxs), max(cxs), max(cxs)-min(cxs)))
    print("接地脚底 y %d~%d（接地线 %d）" % (min(feet), max(feet), GROUND))
    print("鞋底倾角 逐帧 %s" % tilts)
    print("           最大 %.0f°（规格：支撑期 ≤10°、蹬地 ≤30°）" % max([t for t in tilts if t] or [0]))
    ds = []
    for k in range(1, 12):
        best = None
        for x in track[k-1]:
            for y in track[k]:
                d = y - x
                if d < 0 and (best is None or d > best):
                    best = d
        if best is not None:
            ds.append(-best)
    if ds:
        med = float(np.median(ds))
        print("脚锁定 每帧后移 %.1f px / 期望 %.1f px → 达成 %.0f%%（规格 ≥50%%）"
              % (med, 180.0/12, med/(180.0/12)*100))
    else:
        print("脚锁定 量不到（没有向后位移）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
