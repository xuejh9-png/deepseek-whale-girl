#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
桌宠素材自动接收 —— 找新素材 → 合并验收 → 通过才安装。

用法：
    python3 intake-assets.py --dry-run        # 只找 + 验收，不动项目
    python3 intake-assets.py                  # 验收通过则安装（只新增，不覆盖）
    python3 intake-assets.py --accept run     # 明确声明"run 是返工升级，允许覆盖同名"

素材放哪：**桌面 或 下载目录**（zip 包或散装 png 都行），脚本自己去找。

⚠️ 2026-09-22 的事故与修复（这个脚本的核心约束就来自它）
--------------------------------------------------------------------
初版只判断「交付物与项目是否不同」，**没有判断「谁更新」**。
结果：项目里已经是 21:28 交付的返工版 work.png，而桌面上还躺着 20:16 的旧包；
旧包里的旧 work.png 因此被判定为"有变化" → **用旧版覆盖了新版，无备份**。

根因是方法错，不是疏忽：**一个只会"发现差异就写入"的自动化，
在项目比来源更新时必然造成降级覆盖。**

现在的四重防护：
  1. 逐文件分类 new / same / **differs** —— differs **默认跳过**并写通知
  2. 要把 differs 装进去，必须**显式 --accept <剪辑名>**（等于人工确认"这是升级"）
  3. **被 accept 的那一支在验收里不能有任何告警** ——
     返工的目的就是把告警清掉；带着旧告警装进来等于白返工
  4. 安装前把 assets/pet/ 整目录**备份**到 assets/.backup/<时间戳>/

另外：manifest 是**合并**语义（保留已有剪辑，只增补新剪辑），
素材是**移动**而非复制，并清理解压残留。

退出码：0 = 已安装 / 无需安装；1 = 验收不通过
"""
import argparse
import glob
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
PETDIR = os.path.join(HERE, "assets", "pet")
BACKUP = os.path.join(HERE, "assets", ".backup")
VERIFY = os.path.join(HERE, "verify-assets.py")
HOME = os.path.expanduser("~")
SEARCH = [os.path.join(HOME, "Desktop"), os.path.join(HOME, "Downloads")]


def md5(p):
    return hashlib.md5(open(p, "rb").read()).hexdigest()


def inside_project(p):
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
                    if any(n.endswith(".png") and "/pet/" in n for n in z.namelist()):
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
    os.makedirs(dst, exist_ok=True)
    if kind == "zip":
        with zipfile.ZipFile(path) as z:
            for n in z.namelist():
                if n.endswith("/"):
                    continue
                b = os.path.basename(n)
                if b.endswith(".png") or b == "manifest.json":
                    with z.open(n) as s, open(os.path.join(dst, b), "wb") as o:
                        shutil.copyfileobj(s, o)
    else:
        for b in os.listdir(path):
            if b.endswith(".png") or b == "manifest.json":
                shutil.copy2(os.path.join(path, b), os.path.join(dst, b))
    # zip 解出来的文件 mtime 是压缩包里的时间戳，这里统一成"交付到达时间"，
    # 否则压缩包时间戳可能比磁盘上的文件早，导致误判 older
    now = time.time()
    for b in os.listdir(dst):
        os.utime(os.path.join(dst, b), (now, now))
    return sorted(os.listdir(dst))


def classify(files, tmp):
    """逐文件判定：new / same / differs

    ⚠️ 刻意【不按时间戳】判新旧 —— zip 里的时间戳是压缩时的，
    解出来还可能早于磁盘文件；靠 mtime 判"谁更新"会把降级包误判成升级。
    所以策略从"谁更新"改成**更保守的一条**：

      · new    → 项目里没有这个文件 → 安装
      · same   → 字节相同 → 跳过
      · differs→ 项目里已有同名文件但内容不同 → **不自动覆盖**，
                 写通知让人确认（因为无法判断这是"返工升级"还是"旧包降级"）

    自动写入只做"新增"，**永不自动覆盖已有素材**。
    这是 2026-09-22 覆盖事故后定下的硬规则：
    一个只判断"有差异"的自动化，在项目比来源更新时必然造成回退。
    """
    out = {}
    for f in files:
        if not f.endswith(".png"):
            continue
        src, dst = os.path.join(tmp, f), os.path.join(PETDIR, f)
        if not os.path.exists(dst):
            out[f] = "new"
        elif md5(src) == md5(dst):
            out[f] = "same"
        else:
            out[f] = "differs"
    return out


def merge_manifest(pm, dm):
    if not dm:
        return pm, [], []
    out = json.loads(json.dumps(pm))
    added, updated = [], []
    for name, meta in dm.get("clips", {}).items():
        if name in out["clips"]:
            if json.dumps(out["clips"][name], sort_keys=True) != json.dumps(meta, sort_keys=True):
                updated.append(name)
            out["clips"][name] = meta
        else:
            out["clips"][name] = meta
            added.append(name)
    if "spriteSheet" in dm:
        out["spriteSheet"] = dm["spriteSheet"]
    return out, added, updated


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--accept", default="",
                    help="显式声明这几支是返工升级、允许覆盖同名文件（逗号分隔，填剪辑名，如 run）")
    args = ap.parse_args()

    accept = {s.strip() for s in args.accept.split(",") if s.strip()}

    kind, path = find_delivery()
    if not kind:
        print("NO_NEW_ASSETS  没找到待接收的素材包")
        return 0
    print(f"FOUND  {kind}: {path}")

    with tempfile.TemporaryDirectory() as tmp:
        files = stage(kind, path, tmp)
        print(f"  交付内容: {', '.join(files)}")

        pm = json.load(open(os.path.join(PETDIR, "manifest.json"), encoding="utf-8"))
        dm = None
        dp = os.path.join(tmp, "manifest.json")
        if os.path.exists(dp):
            try:
                dm = json.load(open(dp, encoding="utf-8"))
            except Exception as e:
                print(f"  WARN 交付的 manifest 解析失败：{e}")

        # —— 三重防护之一：逐文件判定（只自动新增，绝不覆盖已有素材）——
        cls = classify(files, tmp)
        installable = [f for f, v in cls.items() if v == "new"]
        differs = [f for f, v in cls.items() if v == "differs"]

        # --accept：把被显式指名的返工稿从 differs 提到 installable。
        # 这一步是"人工确认"的机器表达 —— 只有人说了"这是升级"才允许覆盖同名文件。
        accepted = [f for f in differs if os.path.splitext(f)[0] in accept]
        if accepted:
            installable += accepted
            differs = [f for f in differs if f not in accepted]
            print(f"  ACCEPT 人工声明为返工升级、允许覆盖：{', '.join(accepted)}")

        print("  逐文件判定: " + ", ".join(f"{f}={v}" for f, v in sorted(cls.items())))
        if differs:
            print(f"  ⚠ 以下文件项目里已存在且内容不同 → **不自动覆盖**，需人工确认："
                  f"{', '.join(differs)}")
            print("    （无法判断是'返工升级'还是'旧包降级'，交给你决定）")
            print("     确认是返工升级就重跑：python3 intake-assets.py --accept "
                  + ",".join(os.path.splitext(f)[0] for f in differs))
            notice = os.path.join(HOME, "Desktop", "桌宠素材-待人工确认.md")
            with open(notice, "w", encoding="utf-8") as fh:
                fh.write("# 桌宠素材：有文件需要你确认\n\n")
                fh.write(f"检测到交付包：`{path}`\n\n")
                fh.write("以下文件项目里已存在且**内容与交付包不同**，"
                         "程序**没有自动覆盖**（分不清是返工升级还是旧包降级）：\n\n")
                for f in differs:
                    fh.write(f"- `{f}`\n")
                fh.write("\n## 你要做的\n\n")
                fh.write("如果这是**新的返工版**（要采纳），让我重跑一句：\n\n")
                fh.write("```\npython3 intake-assets.py --accept "
                         + ",".join(os.path.splitext(f)[0] for f in differs)
                         + "\n```\n\n")
                fh.write("如果这是**旧包重新投递**（要丢弃），直接删掉它就行。\n\n")
                fh.write("备份在 `assets/.backup/<时间戳>/`，随时可回退。\n")
            print(f"    已写通知：{notice}")

        merged, added, updated = merge_manifest(pm, dm)
        if not installable and not added:
            print("NO_NEW_ASSETS  没有可自动新增的内容，跳过")
            return 0
        print(f"  可安装: {', '.join(installable) or '无'}；manifest 新增 {added or '无'} / 更新 {updated or '无'}")

        # —— 在临时目录里验「合并后的全集」 ——
        merged_dir = os.path.join(tmp, "_merged")
        shutil.copytree(PETDIR, merged_dir)
        for f in installable:
            p = os.path.join(tmp, f)
            try:
                from PIL import Image
                im = Image.open(p)
                if im.size[0] % 320 or im.size[1] % 400:
                    print(f"  SKIP {f}: 尺寸 {im.size} 不是 320×400 的整数倍")
                    continue
            except Exception as e:
                print(f"  SKIP {f}: 打不开 ({e})")
                continue
            shutil.copy2(p, os.path.join(merged_dir, f))
        json.dump(merged, open(os.path.join(merged_dir, "manifest.json"), "w", encoding="utf-8"),
                  ensure_ascii=False, indent=2)

        print("\n--- 验收合并后的全集 ---")
        sys.stdout.flush()
        _r = subprocess.run([sys.executable, VERIFY, "--dir", merged_dir],
                            capture_output=True, text=True)
        _out = (_r.stdout or "") + (_r.stderr or "")
        print(_out, end="")
        sys.stdout.flush()
        if _r.returncode != 0:
            print("\n未通过验收 → 项目保持原样，未安装。请把上面的 FAIL 反馈给制作方。")
            return 1

        # —— 三重防护之三：被 accept 的返工稿不能带着告警装进来 ——
        # verify 的每条 WARN 都是"用户能看得出来的毛病"。返工的目的就是清掉它，
        # 带着旧告警装进来 = 白返工一轮。只卡被 accept 的那几支，
        # 别的素材早有告警（比如 error）不该连累这次交付。
        if accepted:
            own = []
            for line in _out.splitlines():
                s = line.strip()
                if not s.startswith("!"):
                    continue
                for f in accepted:
                    c = os.path.splitext(f)[0]
                    if s.startswith("! %s:" % c) or s.startswith("! %s#" % c):
                        own.append(s[1:].strip())
            if own:
                print(f"\n被声明的返工稿仍有 {len(own)} 条告警 → **不安装**"
                      f"（正式素材保持原样）：")
                for s in own:
                    print(f"  · {s}")
                print("  验收标准见 docs/run-返工说明.md（这批要求哪几条）")
                return 1

        if args.dry_run:
            print("\n(dry-run) 验收通过，未安装。")
            return 0

        # —— 三重防护之二：安装前备份 ——
        stamp = time.strftime("%Y%m%d-%H%M%S")
        bdir = os.path.join(BACKUP, stamp)
        os.makedirs(bdir, exist_ok=True)
        for f in os.listdir(PETDIR):
            shutil.copy2(os.path.join(PETDIR, f), os.path.join(bdir, f))
        print(f"\n--- 安装（已备份到 assets/.backup/{stamp}/）---")

        for f in installable:
            shutil.move(os.path.join(tmp, f), os.path.join(PETDIR, f))
            print(f"  装入 {f}")
        json.dump(merged, open(os.path.join(PETDIR, "manifest.json"), "w", encoding="utf-8"),
                  ensure_ascii=False, indent=2)
        print("  已更新 manifest.json")

        # 同步生成 manifest.js —— 否则 file:// 下（双击直接看）读不到新清单
        subprocess.call([sys.executable, os.path.join(HERE, "build-manifest-js.py")])

    if kind == "dir":
        try:
            shutil.rmtree(os.path.dirname(path))
            print(f"  已清理解压目录 {path}")
        except Exception as e:
            print(f"  WARN 清理未完成：{e}")

    print("\nINSTALLED 安装完成。接下来：跑 pet-runtime.test.html 回归，再 git commit。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
