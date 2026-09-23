#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
把 assets/pet/manifest.json 转成 assets/pet/manifest.js。

为什么要这一步：
    浏览器对 file:// 页面的 fetch 会按 CORS 拦掉，
    所以「双击 pet.html 直接看」时读不到 manifest.json。
    换成 <script src="manifest.js"> 就没有这个问题（脚本不受同源限制）。

    manifest.json 仍然是**唯一权威**（素材方交付的就是它）。
    这个脚本只是把它包一层，别手改 manifest.js。

用法：
    python3 build-manifest-js.py            # 转换
    python3 build-manifest-js.py --check    # 只检查是否已同步（不写文件）
"""
import argparse
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PETDIR = os.path.join(HERE, "assets", "pet")
SRC = os.path.join(PETDIR, "manifest.json")
DST = os.path.join(PETDIR, "manifest.js")

HEADER = ("/* 由 build-manifest-js.py 从 manifest.json 生成 —— 不要手改这个文件。\n"
          "   用途：file:// 下 fetch 会被 CORS 拦，改用 <script> 加载。 */\n")


def render(man):
    return HEADER + "window.PET_MANIFEST = " + json.dumps(man, ensure_ascii=False, indent=2) + ";\n"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="只检查是否已同步")
    args = ap.parse_args()

    if not os.path.exists(SRC):
        print(f"找不到 {SRC}")
        return 1
    try:
        man = json.load(open(SRC, encoding="utf-8"))
    except Exception as e:
        print(f"manifest.json 解析失败：{e}")
        return 1

    want = render(man)
    have = open(DST, encoding="utf-8").read() if os.path.exists(DST) else None

    if args.check:
        if have == want:
            print(f"已同步（{len(man.get('clips', {}))} 个剪辑）")
            return 0
        print("未同步：manifest.json 变过，需要重新生成 manifest.js")
        return 1

    if have == want:
        print(f"已是最新，无需重写（{len(man.get('clips', {}))} 个剪辑）")
        return 0

    with open(DST, "w", encoding="utf-8") as fh:
        fh.write(want)
    print(f"已生成 {os.path.relpath(DST, HERE)}（{len(man.get('clips', {}))} 个剪辑）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
