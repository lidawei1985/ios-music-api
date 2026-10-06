# -*- coding: utf-8 -*-
"""把内核 app.html 发布到云端仓库的 data/kernel/，供 App 静默热更新拉取。

⚠️ 当前**不是**主通道（备份用）。正式热更走 iOS 仓库：
   内核 KUP.base = https://gcore.jsdelivr.net/gh/lidawei1985/ios-music-app@main/kernel/
   由 .github/workflows/build-ipa.yml 打壳后自动发布 kernel/dist.html + kernel.json。
   本脚本是更早的通道（发到 cloud 的 data/kernel/），保留以便 iOS 仓库不可用时兜底。
   data/kernel/ 已被 .gitignore 挡住 —— 两份真源并存会让人分不清手机到底拉哪份。

产物：
  data/kernel/app.html    —— 打了 data-shell="ios" 壳标记的内核（与出包同一份）
  data/kernel/kernel.json —— {version, file, bytes, sha256, updated}

version 怎么来：默认用内核内容 sha256 前 12 位（内容变了版本必然变，
不需要人工记版本号，也不会出现「改了内容忘了改版本」）。可用 --version 强制指定。

用法：
  python tools/publish_kernel.py                 # 从 ios/kernel/app.html 发布
  python tools/publish_kernel.py --stage         # 只写出文件，不提交
  python tools/publish_kernel.py --version v78   # 指定版本号
"""
import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)                      # .../musicdl/cloud
PROJ = os.path.dirname(ROOT)                      # .../musicdl
DST = os.path.join(ROOT, "data", "kernel")

# 内核来源：本机优先 prototype（真源），CI 里没有则用 ios/kernel
_SRC_CANDIDATES = [
    os.path.join(PROJ, "prototype", "app.html"),
    os.path.join(PROJ, "ios", "kernel", "app.html"),
]


def inject_shell(html, plat="ios"):
    """与 ios/scripts/pack.py 完全一致的壳标记注入（保证热更内核 == 出包内核）"""
    if 'data-shell=' not in html.split('>', 1)[0]:
        html = re.sub(r'<html([^>]*)>', r'<html\1 data-shell="%s">' % plat, html, count=1)
    boot = '<script>window.__PLATFORM__=%s;</script>' % ('"%s"' % plat)
    html = re.sub(r'(<head[^>]*>)', r'\1' + boot, html, count=1)
    return html


def sha256(b):
    return hashlib.sha256(b).hexdigest()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--stage", action="store_true", help="只写文件，不 git 提交")
    ap.add_argument("--version", default="", help="强制版本号（默认取内容 sha 前 12 位）")
    ap.add_argument("--src", default="", help="指定内核源文件")
    a = ap.parse_args()

    src = a.src or next((p for p in _SRC_CANDIDATES if os.path.exists(p)), "")
    if not src or not os.path.exists(src):
        print("RESULT: FAIL —— 找不到内核源，试过：", _SRC_CANDIDATES)
        sys.exit(1)
    raw = open(src, encoding="utf-8").read()
    out = inject_shell(raw, "ios")
    data = out.encode("utf-8")

    # 硬门禁（与 CI 出包同一套，保证热更内核是「真内核」）
    if len(data) < 2_000_000:
        print("RESULT: FAIL —— 内核仅 %d 字节，疑似被裁剪" % len(data))
        sys.exit(1)
    if 'data-shell="ios"' not in out[:4096]:
        print("RESULT: FAIL —— 缺 data-shell=ios 壳标记")
        sys.exit(1)
    if "__PLATFORM__" not in out[:4096]:
        print("RESULT: FAIL —— 缺平台标识注入")
        sys.exit(1)
    if "</html>" not in out[-2048:]:
        print("RESULT: FAIL —— 结尾缺 </html>，结构不闭合")
        sys.exit(1)

    os.makedirs(DST, exist_ok=True)
    ver = a.version or sha256(data)[:12]
    dst_html = os.path.join(DST, "app.html")
    dst_meta = os.path.join(DST, "kernel.json")

    # 内容没变就不重复提交（避免 CI 空转）
    old_ver = ""
    if os.path.exists(dst_meta):
        try:
            old_ver = json.load(open(dst_meta, encoding="utf-8")).get("version") or ""
        except Exception:
            pass
    old_bytes = open(dst_html, "rb").read() if os.path.exists(dst_html) else b""
    if old_bytes == data and old_ver == ver:
        print("RESULT: SKIP —— 云端已是最新（v%s，%d 字节）" % (ver, len(data)))
        return

    with open(dst_html, "wb") as f:
        f.write(data)
    meta = {
        "version": ver,
        "file": "app.html",
        "bytes": len(data),
        "sha256": sha256(data),
        "updated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    with open(dst_meta, "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)
    print("已写出:")
    print("  %s  %.2f MB" % (dst_html, len(data) / 1048576))
    print("  %s  version=%s" % (dst_meta, ver))

    if a.stage:
        print("RESULT: STAGE OK（未提交）")
        return

    # 提交（本机 git 到 github.com 可能不通，交给调用方决定是否用 Git Data API）
    try:
        subprocess.run(["git", "add", "data/kernel"], cwd=ROOT, check=True)
        r = subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=ROOT)
        if r.returncode == 0:
            print("RESULT: SKIP —— 无实际变更")
            return
        subprocess.run(["git", "commit", "-m",
                        "内核热更 v%s（%d 字节）：App 端静默拉取" % (ver, len(data))],
                       cwd=ROOT, check=True)
        print("RESULT: COMMIT OK —— 已本地提交，待推送")
    except subprocess.CalledProcessError as e:
        print("RESULT: FAIL —— git 操作失败:", e)
        sys.exit(1)


if __name__ == "__main__":
    main()
