"""小步快跑版装置：固定身体（12 帧共用）+ 分离的腿（小步、脚贴地）。

与 build-run-rig.py 的区别：腿不再从旧素材里取（那版是"大步收腿"，脚抬太高导致
总高变化 16%，违反规格的 ≤3%），改用**新生成的 6 张小步腿姿势**。

规格对照：
  · 头/身体/手臂/裙子 12 帧完全一致  → 身体像素共用
  · 整体高度变化 ≤3%                  → 脚统一锚在接地线，腾空只抬 4px
  · 躯干中心 X 固定、不漂移            → 身体固定、腿按身体中线水平对齐
  · 7-12 帧严格镜像 1-6 帧            → 第 7~12 帧的腿用 1~6 帧水平镜像
  · 腿长缩短 15~20%                   → 腿部竖直缩放
"""
import glob
import os
import sys

import numpy as np
from PIL import Image

FW, FH, COLS = 320, 400, 6
CELL_W, CELL_H, ROWS = 320, 400, 2
CUT = 306               # 身体 / 腿 分界线（参考素材实测：裙摆下沿 ~305）
GROUND = 356            # 接地线（脚底）
CHAR_REF_H = 258        # 基准角色高（归一化用）
LEG_TAKE = 78           # 从归一化后的角色底部取多少 px 作为腿部精灵
LEG_SCALE_Y = 0.85      # 腿竖直缩短 18%
AIR_LIFT = 4            # 腾空帧抬升（要小，控高度变化 ≤3%）


def keyout(path):
    im = Image.open(path).convert("RGBA")
    a = np.array(im).astype(int)
    mn = a[:, :, :3].min(axis=2)
    mx = a[:, :, :3].max(axis=2)
    bright = (mn >= 235) & ((mx - mn) <= 10)
    seed = np.zeros_like(bright)
    seed[0, :] = bright[0, :]; seed[-1, :] = bright[-1, :]
    seed[:, 0] = bright[:, 0]; seed[:, -1] = bright[:, -1]
    while True:
        g = seed.copy()
        g[1:, :] |= seed[:-1, :]; g[:-1, :] |= seed[1:, :]
        g[:, 1:] |= seed[:, :-1]; g[:, :-1] |= seed[:, 1:]
        g &= bright
        if np.array_equal(g, seed):
            break
        seed = g
    return Image.fromarray(
        np.dstack([a[:, :, :3].astype(np.uint8),
                   np.where(seed, 0, 255).astype(np.uint8)]), "RGBA")


def tight(im):
    a = np.array(im.split()[-1]) > 128
    ys = np.where(a.any(axis=1))[0]
    xs = np.where(a.any(axis=0))[0]
    return im.crop((xs.min(), ys.min(), xs.max()+1, ys.max()+1))


def main():
    src_dir = sys.argv[1] if len(sys.argv) > 1 else "/tmp/workchk/legs6"
    out_path = sys.argv[2] if len(sys.argv) > 2 else "/tmp/workchk/run-small.png"
    ref = Image.open("assets/pet/run.png").convert("RGBA")

    # ---- 身体：取参考素材的一帧，切掉分界线以下（12 帧共用）----
    body = ref.crop((0, 0, FW, FH))
    ba = np.array(body)
    ba[CUT:, :, 3] = 0
    body = Image.fromarray(ba)
    bb = np.where(np.array(body.split()[-1]) > 128)
    body_top = bb[0].min()

    # ---- 腿：新生成的 6 张小步姿势 ----
    files = sorted(glob.glob(os.path.join(src_dir, "*.png")), key=os.path.getmtime)[:6]
    legs = []
    print("=== 腿部精灵（归一化到角色高 %d 后取底部 %d px）===" % (CHAR_REF_H, LEG_TAKE))
    for i, f in enumerate(files):
        im = tight(keyout(f))
        k = CHAR_REF_H / float(im.height)
        im = im.resize((max(1, int(round(im.width*k))), CHAR_REF_H), Image.LANCZOS)
        leg = im.crop((0, CHAR_REF_H - LEG_TAKE, im.width, CHAR_REF_H))
        la = np.array(leg.split()[-1]) > 128
        ys = np.where(la.any(axis=1))[0]
        xs = np.where(la.any(axis=0))[0]
        off = xs.min() - im.width // 2      # 腿相对角色中线的水平偏移
        leg = leg.crop((xs.min(), ys.min(), xs.max()+1, ys.max()+1))
        legs.append((leg, off))
        print("  leg%d  %3dx%3d  相对中线偏移 %+d  像素 %5d"
              % (i+1, leg.width, leg.height, off, int(la.sum())))

    # 腿的缩短量：以"最长的一条腿"为基准算身体下移量
    ref_leg_h = max(l.height for l, _ in legs)
    shorten = int(round(ref_leg_h * (1 - LEG_SCALE_Y)))
    print()
    print("参考腿高 %d → 缩短 %d px；身体整体下移 %d px" % (ref_leg_h, shorten, shorten))

    # ---- 合成 12 帧 ----
    body_mid = 160
    out = Image.new("RGBA", (CELL_W*COLS, CELL_H*ROWS), (0, 0, 0, 0))
    print()
    print("%-4s %-16s %-10s %s" % ("帧", "腿来源", "脚底 y", "说明"))
    for i in range(12):
        beat = i % 6
        mirror = i >= 6
        L, off = legs[beat]
        nw = L.width
        nh = max(1, int(round(L.height * LEG_SCALE_Y)))
        t = L.resize((nw, nh), Image.LANCZOS)
        if mirror:
            t = t.transpose(Image.FLIP_LEFT_RIGHT)
        lift = AIR_LIFT if (i+1) in (5, 11) else 0
        foot_y = GROUND - lift
        # 水平：按"腿相对角色中线的偏移"贴。
        # 镜像时整个腿的包围盒要翻到中线另一侧：left = 中线 - 偏移 - 宽度
        left = body_mid + off if not mirror else body_mid - off - nw
        r, c = divmod(i, COLS)
        ox, oy = c*CELL_W, r*CELL_H
        out.alpha_composite(body, (ox, oy + shorten))
        out.alpha_composite(t, (ox + left, oy + foot_y - nh + 1))
        print("%-4d %-16s %-10d %s" % (i+1, "leg%d%s" % (beat+1, "（镜像）" if mirror else ""),
                                       foot_y, "腾空抬 %dpx" % lift if lift else ""))

    out.save(out_path)
    print()
    print("已写出:", out_path, out.size)

    # ---- 自查 ----
    B = np.array(out.split()[-1]) > 128
    hs, cxs, body_hash = [], [], []
    for i in range(12):
        r, c = divmod(i, COLS)
        m = B[r*CELL_H:(r+1)*CELL_H, c*CELL_W:(c+1)*CELL_W]
        ys = np.where(m.any(axis=1))[0]
        xs = np.where(m.any(axis=0))[0]
        hs.append(ys.max()-ys.min()+1)
        cxs.append((xs.min()+xs.max())/2.0)
        body_hash.append(hash(m[:CUT+shorten-6].tobytes()))
    print()
    print("=== 规格自查 ===")
    print("  身高 %d~%d → 变化 %.1f%%   要求 ≤3%%   %s"
          % (min(hs), max(hs), (max(hs)-min(hs))/float(np.median(hs))*100,
             "✓" if (max(hs)-min(hs))/float(np.median(hs))*100 <= 3 else "✗"))
    print("  中心 x %.1f~%.1f → 漂移 %.1f px   要求 0      %s"
          % (min(cxs), max(cxs), max(cxs)-min(cxs),
             "✓" if max(cxs)-min(cxs) <= 1 else "✗"))
    print("  身体区域 12 帧一致：%s" % ("✓" if len(set(body_hash)) == 1 else "✗（%d 种）" % len(set(body_hash))))
    return 0


if __name__ == "__main__":
    sys.exit(main())
