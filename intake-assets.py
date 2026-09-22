#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
桌宠素材自动接收 —— 找新素材 → 合并验收 → 通过才安装。

用法：
    python3 intake-assets.py --dry-run    # 只找 + 验收，不动项目
    python3 intake-assets.py              # 验收通过则安装（merge manifest + 覆盖 PNG）

安全设计：
    · **先在临时目录里验合并后的全集**，通过了才动项目 ——
      不通过时项目保持原样，不会装进半成品
    · manifest 是**合并**语义（保留项目已有剪辑，只增补新剪辑），
      不会被制作方按批交来的清单覆盖丢条目
    · 素材是**移动**进项目（不是复制），并清理解压残留，保证全机只有一份

退出码：0 = 已安装或无需安装；1 = 验收不通过
"""
import argparse
import glob
import json
import os
import shutil
import subprocess
import sys
import tempfile
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
PETDIR = os.path.join(HERE, "assets", "pet")
VERIFY = os.path.join(HERE, "verify-assets.py")
HOME = os.path.expanduser("~")
SEARCH = [os.path.join(HOME, "Desktop"), os.path.join(HOME, "Downloads")]


def inside_project(p):
    """排除项目自身 —— 否则会把 assets/pet/ 自己当成"新交付"。"""
    a, b = os.path.abspath(p), os.path.abspath(HERE)
    return a == b or a.startswith(b + os.sep)


def find_delivery():
    """返回 (类型, 路径)。优先 zip，其次裸目录。排除项目自身。"""
    cands = []
    for base in SEARCH:
        if not os.path.isdir(base):
            continue
        for zp in glob.glob(os.path.join(base, "*.zip")):
            if inside_project(zp):
                continue
            try:
                with zipfile.ZipFile(zp) as z:
                    names = z.namelist()
                if any(n.endswith(".png") and "/pet/" in n for n in names):
                    cands.append((os.path.getmtime(zp), "zip", zp))
            except Exception:
                pass
        for root, dirs, files in os.walk(base):
            dirs[:] = [d for d in dirs if not inside_project(os.path.join(root, d))]
            if inside_project(root):
                continue
            if os.path.basename(root) != "pet":
                continue
            if any(f.endswith(".png") for f in files):
                cands.append((os.path.getmtime(root), "dir", root))
            dirs[:] = []
    if not cands:
        return None, None
    cands.sort(reverse=True)
    _, kind, path = cands[0]
    return kind, path


def stage(kind, path, dst):
    """把交付物摊到 dst 目录（只取 png + manifest.json）。"""
    os.makedirs(dst, exist_ok=True)
    if kind == "zip":
        with zipfile.ZipFile(path) as z:
            for n in z.namelist():
                if n.endswith("/"):
                    continue
                b = os.path.basename(n)
                if b.endswith(".png") or b == "manifest.json":
                    with z.open(n) as src, open(os.path.join(dst, b), "wb") as out:
                        shutil.copyfileobj(src, out)
    else:
        for b in os.listdir(path):
            if b.endswith(".png") or b == "manifest.json":
                shutil.copy2(os.path.join(path, b), os.path.join(dst, b))
    return sorted(f for f in os.listdir(dst))


def merge_manifest(project_man, delivery_man):
    """合并：保留项目已有剪辑，增补交付清单里的剪辑。"""
    if not delivery_man:
        return project_man, []
    out = json.loads(json.dumps(project_man))
    added, updated = [], []
    for name, meta in delivery_man.get("clips", {}).items():
        if name in out["clips"]:
            if json.dumps(out["clips"][name], sort_keys=True) != json.dumps(meta, sort_keys=True):
                updated.append(name)
            out["clips"][name] = meta
        else:
            out["clips"][name] = meta
            added.append(name)
    # 顶层 spriteSheet 以交付为准（尺寸/锚点若变了要跟上）
    if "spriteSheet" in delivery_man:
        out["spriteSheet"] = delivery_man["spriteSheet"]
    return out, added, updated


def md5(p):
    import hashlib
    return hashlib.md5(open(p, "rb").read()).hexdigest()


def whats_new(files, tmp, delivery_man, project_man):
    """交付内容相对项目到底有没有变化。全都一样就是"旧包重复发现"。"""
    changed = []
    for f in files:
        if not f.endswith(".png"):
            continue
        src, dst = os.path.join(tmp, f), os.path.join(PETDIR, f)
        if not os.path.exists(dst):
            changed.append(f"{f}（新）")
        elif md5(src) != md5(dst):
            changed.append(f"{f}（有改动）")
    if delivery_man:
        for name in delivery_man.get("clips", {}):
            if name not in project_man.get("clips", {}):
                changed.append(f"剪辑 {name}（新）")
    return changed


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    kind, path = find_delivery()
    if not kind:
        print("NO_NEW_ASSETS  没找到待接收的素材包")
        return 0
    print(f"FOUND  {kind}: {path}")

    delivery_files = None
    with tempfile.TemporaryDirectory() as tmp:
        files = stage(kind, path, tmp)
        delivery_files = files
        print(f"  交付内容: {', '.join(files)}")

        project_man = json.load(open(os.path.join(PETDIR, "manifest.json"), encoding="utf-8"))
        delivery_man = None
        dman_path = os.path.join(tmp, "manifest.json")
        if os.path.exists(dman_path):
            try:
                delivery_man = json.load(open(dman_path, encoding="utf-8"))
            except Exception as e:
                print(f"  WARN 交付的 manifest 解析失败：{e}（将沿用项目现有清单）")

        # —— 是不是"旧包重复发现"？是的话直接退出，别每次轮询都报一遍
        changed = whats_new(files, tmp, delivery_man, project_man)
        if not changed:
            print("NO_NEW_ASSETS  交付内容与已装素材完全一致（旧包重复发现），跳过")
            return 0
        print(f"  实际有变化: {', '.join(changed)}")

        # 合并出「全集」到临时目录：先放项目现有的，再用交付的覆盖
        merged_dir = os.path.join(tmp, "_merged")
        shutil.copytree(PETDIR, merged_dir)
        for f in files:
            if not f.endswith(".png"):
                continue
            try:
                from PIL import Image
                im = Image.open(os.path.join(tmp, f))
                if im.size[0] % 320 or im.size[1] % 400:
                    print(f"  SKIP {f}: 尺寸 {im.size} 不是 320×400 的整数倍")
                    continue
            except Exception as e:
                print(f"  SKIP {f}: 打不开 ({e})")
                continue
            shutil.copy2(os.path.join(tmp, f), os.path.join(merged_dir, f))

        merged, added, updated = merge_manifest(project_man, delivery_man) if delivery_man \
            else (project_man, [], [])
        json.dump(merged, open(os.path.join(merged_dir, "manifest.json"), "w", encoding="utf-8"),
                  ensure_ascii=False, indent=2)
        print(f"  manifest 合并: 新增 {added or '无'} / 更新 {updated or '无'} / "
              f"保留 {[k for k in project_man['clips'] if k not in (added or [])]}")

        # 验收「合并后的全集」
        print("\n--- 验收合并后的全集 ---")
        sys.stdout.flush()
        rc = subprocess.call([sys.executable, VERIFY, "--dir", merged_dir])
        sys.stdout.flush()
        if rc != 0:
            print("\n未通过验收 → 项目保持原样，未安装。请把上面的 FAIL 反馈给制作方。")
            return 1

        if args.dry_run:
            print("\n(dry-run) 验收通过，未安装。")
            return 0

        # 安装
        print("\n--- 安装 ---")
        os.makedirs(PETDIR, exist_ok=True)
        for f in files:
            if f.endswith(".png"):
                src = os.path.join(tmp, f)
                if os.path.exists(src):
                    shutil.move(src, os.path.join(PETDIR, f))
                    print(f"  装入 {f}")
        json.dump(merged, open(os.path.join(PETDIR, "manifest.json"), "w", encoding="utf-8"),
                  ensure_ascii=False, indent=2)
        print("  已更新 manifest.json")

    # 清理交付残留（桌面/下载里的解压目录），只保留 zip 归档
    if kind == "dir":
        try:
            shutil.rmtree(os.path.dirname(path))     # 删掉 assets/ 那一层
            print(f"  已清理解压目录 {path}")
        except Exception as e:
            print(f"  WARN 清理未完成：{e}")

    print("\nINSTALLED 安装完成。接下来：跑 pet-runtime.test.html 回归，再 git commit。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
