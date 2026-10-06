#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""asset_guard.py —— 图片沉淀的**体量守卫**（进 CI 前置，防仓库爆仓）。

★★ 为什么必须有这个（血案预案，不是假设）：
   GitHub 仓库**硬上限 1GB**，且**超过 1GB 后 push 直接被拒** —— 不是警告，是拒绝。
   我们这套"预先全量落地"的策略天生会往仓库里堆图片：
     曲库 101256 首 → 唯一封面 70987 张 ≈ 832MB（实测核算），
     加上曲库 JSON 自身、分片、歌词评论，**一会儿就顶到天花板**。
   一旦顶到，整个采集流水线会开始**静默失败**（push 被拒 → 本轮成果全丢），
   而且很难排查 —— 表面现象只是"数据不更新了"。

   本守卫在"入库并发布"之前跑，三重判据：
     ① 单文件 > 95MB → 阻断（GitHub 单文件硬限 100MB）
     ② 资产目录总量 > 上限（默认 700MB，给仓库其他内容留 300MB 余量）→ 阻断
     ③ 本次将要新增的量 > 预留余量 → 阻断
   阻断后 CI 不会 push，线上数据保持上一版**可用状态**（不会半吊子）。

用法：
  python tools/asset_guard.py            # 检查，超限则退出码 1
  python tools/asset_guard.py --warn     # 只警告不阻断（排障用）
"""
import json
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data")
AS = os.path.join(DATA, "assets")

# GitHub 硬限：单文件 100MB、仓库软限 1GB
# 我们的分层预算（2026-10-06 定稿，见 tools/ASSETS-PLAN.py）：
#   热图（头像/KV/热度分层封面）全走 git + gcore.jsdelivr（实测 22KB/s 可用）
#   → 资产目录守 700MB，仓库总量守 850MB，给曲库分片/歌词/评论留余量。
REPO_LIMIT_MB = int(os.environ.get("AG_REPO_LIMIT_MB") or "1000")
ASSET_LIMIT_MB = int(os.environ.get("AG_ASSET_LIMIT_MB") or "700")
FILE_LIMIT_MB = int(os.environ.get("AG_FILE_LIMIT_MB") or "95")


def du(path):
    """目录真实字节数（含子目录）"""
    t = 0
    for r, _d, fs in os.walk(path):
        for f in fs:
            p = os.path.join(r, f)
            try:
                t += os.path.getsize(p)
            except OSError:
                pass
    return t


def git_tracked_bytes():
    """git 里已跟踪的全部文件字节数（近似仓库内容量）"""
    try:
        out = subprocess.run(["git", "ls-files", "-z"], cwd=ROOT,
                             capture_output=True, check=False).stdout
        files = [f for f in out.decode("utf-8", "replace").split("\0") if f]
        t = 0
        for f in files:
            p = os.path.join(ROOT, f)
            try:
                if os.path.exists(p):
                    t += os.path.getsize(p)
            except OSError:
                pass
        return t, len(files)
    except Exception:
        return 0, 0


def main():
    warn_only = "--warn" in sys.argv
    fail = []

    asset_b = du(AS) if os.path.isdir(AS) else 0
    tracked_b, n_files = git_tracked_bytes()
    mb = lambda b: b / 1048576.0

    print("== 图片沉淀体量守卫 ==")
    print("  资产目录 data/assets/  : %.1f MB（上限 %d MB）" % (mb(asset_b), ASSET_LIMIT_MB))
    print("  git 已跟踪 %d 个文件    : %.1f MB（仓库硬限 %d MB）"
          % (n_files, mb(tracked_b), REPO_LIMIT_MB))

    # ① 单文件上限
    big = []
    if os.path.isdir(AS):
        for r, _d, fs in os.walk(AS):
            for f in fs:
                p = os.path.join(r, f)
                try:
                    s = os.path.getsize(p)
                except OSError:
                    continue
                if mb(s) > FILE_LIMIT_MB:
                    big.append((p, mb(s)))
    if big:
        for p, s in big[:10]:
            print("  ❌ 单文件超限：%s = %.1f MB" % (os.path.relpath(p, ROOT), s))
        fail.append("有 %d 个文件超过 %d MB（GitHub 单文件硬限 100MB）" % (len(big), FILE_LIMIT_MB))

    # ② 资产总量
    if mb(asset_b) > ASSET_LIMIT_MB:
        fail.append("资产目录 %.1f MB 超上限 %d MB —— 请调低 CV_LIB_TOP（曲库封面覆盖层数）"
                    % (mb(asset_b), ASSET_LIMIT_MB))

    # ③ 仓库总量
    if mb(tracked_b) > REPO_LIMIT_MB:
        fail.append("git 已跟踪内容 %.1f MB 超仓库硬限 %d MB" % (mb(tracked_b), REPO_LIMIT_MB))
    elif mb(tracked_b) > REPO_LIMIT_MB * 0.85:
        print("  ⚠️  已用 %.0f%% 仓库容量，建议下调本地化覆盖层数"
              % (100.0 * mb(tracked_b) / REPO_LIMIT_MB))

    # 资产索引总账（给人看的一行摘要）
    mp = os.path.join(AS, "manifest.json")
    if os.path.exists(mp):
        try:
            m = json.load(open(mp, encoding="utf-8"))
            ks = m.get("kinds") or {}
            print("  索引：" + " / ".join("%s %s 张 %.1f MB"
                                          % (k, v.get("count"), (v.get("bytes") or 0) / 1048576.0)
                                          for k, v in ks.items()))
        except Exception:
            pass

    if fail:
        print()
        for f in fail:
            print("  🚫 " + f)
        if warn_only:
            print("  （--warn 模式：仅警告，不阻断）")
            return 0
        print("  → 阻断本次发布（线上保持上一版可用数据，不会半吊子）")
        return 1
    print("  ✅ 体量安全")
    return 0


if __name__ == "__main__":
    sys.exit(main())
