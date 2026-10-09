#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""图片资产分仓（lidawei1985/dwg-assets）的同步与发布工具。

━━ 为什么要分仓 ━━
主仓 `ios-music-api` 同时装「曲库 + 流水线 + 图片资产」，实测已 1002MB，撞 GitHub 1GB 软限。
三类产物里只有**图片**会无限增长（曲库/歌词是分片的，片数是可预期的），
所以把图片拆到独立仓 → 每个仓各有一份 1GB 预算；不够就再开 dwg-assets-b / -c
（同一套机制，端上只是多一档基址）。主人在星幕项目上已经这么干过（fc-img-xingmu-a/b 各 700MB+），
且实测 jsDelivr 能正常服务 745MB 的仓（gcore 200 / 30KB / 2.0s）——不是纸面推演。

━━ 数据流 ━━
  data/assets/            （真源：localize.py 每轮往里写 WebP + 索引）
      │  mirror（按 大小+mtime 增量，不删远端 .git）
      ▼
  <work>/                 （dwg-assets 的克隆；默认 ./.assets-publish）
      │  commit + push
      ▼
  github.com/lidawei1985/dwg-assets@main   →  jsDelivr  →  端上 ASSETBASE

━━ 端上布局契约（改了必须同步 kernel/app.html）━━
  dwg-assets/ 根目录 ≡ data/assets/ 的内容
    cv/*.webp  av/*.webp  kv/*.webp
    cv.json cv.js av.json av.js kv.json kv.js manifest.json

━━ 用法 ━━
  python tools/asset_repo.py status                  # 看本机/远端状态
  python tools/asset_repo.py all --yes               # 同步 + 镜像 + 提交推送（CI 走这条）
  python tools/asset_repo.py sync                    # 只克隆/更新 work 目录
  python tools/asset_repo.py mirror                  # 只把 data/assets 增量镜像到 work
  python tools/asset_repo.py publish --yes           # 只提交推送

安全护栏（防误清空）：
  · cv/*.webp 少于 MIN_CV 张 → 拒绝发布（说明源目录坏/没 checkout 全）
  · 工作区无变更 → 静默成功（幂等）
  · 远端有他人提交 → rebase 重试，最多 4 次
"""

import os
import shutil
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data")
AS = os.path.join(DATA, "assets")

# ★ work 目录默认是独立目录（不放在 data/assets 里）：主仓此刻仍跟踪 data/assets，
#   一旦在里面放 .git，父仓会把它当 gitlink，`git add -A data` 会顺手删掉两万个文件。
WORK = os.environ.get("ASSET_WORK") or os.path.join(ROOT, ".assets-publish")

REPO_SSH = "ssh://git@ssh.github.com:443/lidawei1985/dwg-assets.git"
REPO_HTTPS = "https://github.com/lidawei1985/dwg-assets.git"

KINDS = ("cv", "av", "kv")
MIN_CV = 5000          # cv 张数下限护栏
MIN_TOTAL = 6000       # 三类合计下限

# dwg-assets 自己的 .gitignore（prune.log 每轮十几万字，不进仓）
WORK_GITIGNORE = """# 运行时日志（本仓只存图与索引，日志不入库）
prune.log
*.tmp
_bak_*
"""

# 只镜像这些（远端多余的一律不动，避免误删）
SKIP_TOP = {".git", "prune.log"}


def log(msg):
    print(msg, flush=True)


def run(args, cwd=None, check=True, capture=False):
    env = dict(os.environ)
    key = os.environ.get("ASSETS_KEY_FILE")
    if key and os.path.exists(key):
        env["GIT_SSH_COMMAND"] = (
            "ssh -i %s -o IdentitiesOnly=yes -o StrictHostKeyChecking=no "
            "-o UserKnownHostsFile=/dev/null -o LogLevel=ERROR" % key
        )
    r = subprocess.run(args, cwd=cwd, env=env, capture_output=capture, text=True)
    if capture:
        return r
    if check and r.returncode != 0:
        log("!! 命令失败(%d): %s" % (r.returncode, " ".join(args)))
        sys.exit(r.returncode)
    return r


def git(*args, **kw):
    return run(["git"] + list(args), cwd=WORK, **kw)


def count_webp(d):
    n = 0
    for k in KINDS:
        p = os.path.join(d, k)
        if os.path.isdir(p):
            n += sum(1 for f in os.listdir(p) if f.endswith(".webp"))
    return n


def _same(a, b):
    return os.path.abspath(a) == os.path.abspath(b)


def pull_in():
    """把 dwg-assets 拉到 data/assets（CI 前置步：主仓已不再跟踪 data/assets）。

    ★★ 失败必须硬失败（exit 4），**绝不能静默继续**：
       若 data/assets 是空的而 localize 照跑，它会从零重建索引（只含本轮那 1500 条），
       线上两万张封面的映射当场清空 —— 这是本方案唯一的高危失效路径。
       所以宁可本轮整轮不跑，也不能带着空索引往下走。
    """
    key = os.environ.get("ASSETS_KEY_FILE")
    if not key or not os.path.exists(key):
        log("!! 缺 ASSETS_KEY_FILE（部署密钥）→ 拒绝继续（防空索引覆盖线上映射）")
        sys.exit(4)
    if os.path.isdir(os.path.join(AS, ".git")):
        log("data/assets 已是分仓工作副本 → fetch 更新")
        git_as("remote", "set-url", "origin", REPO_SSH, check=False)
        r = git_as("fetch", "--depth=1", "origin", "main", capture=True, check=False)
        if r.returncode != 0:
            log("!! fetch 失败：%s" % ((r.stderr or "")[:300],))
            sys.exit(4)
        r = git_as("reset", "--hard", "origin/main", capture=True, check=False)
        if r.returncode != 0:
            log("!! reset --hard origin/main 失败：%s" % ((r.stderr or "")[:300],))
            sys.exit(4)
    else:
        tmp = AS + ".clone"
        if os.path.isdir(tmp):
            shutil.rmtree(tmp)
        log("克隆 dwg-assets → data/assets")
        r = run(["git", "clone", "--depth=1", REPO_SSH, tmp], capture=True, check=False)
        if r.returncode != 0:
            log("!! 克隆失败：%s" % ((r.stderr or "")[:400],))
            sys.exit(4)
        if os.path.isdir(AS):
            shutil.rmtree(AS)
        os.makedirs(os.path.dirname(AS), exist_ok=True)
        shutil.move(tmp, AS)
    n = count_webp(AS)
    log("  data/assets 就绪：%d 张 webp" % n)
    if n < MIN_TOTAL:
        log("!! 分仓里只有 %d 张图（下限 %d）→ 拒绝继续" % (n, MIN_TOTAL))
        sys.exit(4)
    return True


def git_as(*args, **kw):
    return run(["git"] + list(args), cwd=AS, **kw)


def ensure_clone():
    """work 目录不是仓库就克隆；是仓库就 fetch（浅克隆，省时间）。"""
    if os.path.isdir(os.path.join(WORK, ".git")):
        r = run(["git", "-C", WORK, "fetch", "--depth=1", "origin", "main"],
                capture=True, check=False)
        if r.returncode != 0:
            log("  fetch 失败（远端可能还是空仓）→ 忽略")
        return False
    os.makedirs(os.path.dirname(WORK) or ".", exist_ok=True)
    if os.path.isdir(WORK):
        # ★ WORK 就是真源目录时**绝不删**（那是在删两万张图）
        if _same(WORK, AS):
            log("!! %s 不是 git 仓且它就是源目录 → 请先跑 pull-in" % WORK)
            sys.exit(4)
        shutil.rmtree(WORK)
    log("克隆 dwg-assets → %s" % WORK)
    # 空仓克隆会失败（没有 main）→ 回落到 init
    r = run(["git", "clone", "--depth=1", REPO_SSH, WORK], capture=True, check=False)
    if r.returncode != 0:
        log("  远端尚无 main（空仓）→ 就地 init")
        if os.path.isdir(WORK):
            shutil.rmtree(WORK)
        os.makedirs(WORK, exist_ok=True)
        run(["git", "init", "-q", "-b", "main"], cwd=WORK)
        git("remote", "add", "origin", REPO_SSH, check=False)
        return True
    git("config", "user.name", "dwg-assets-bot")
    git("config", "user.email", "dwg-assets-bot@users.noreply.github.com")
    return True


def mirror():
    """把 data/assets 增量镜像到 WORK（按文件大小判断，不删远端已有）。"""
    if _same(WORK, AS):
        log("  work 就是源目录本身（分仓就地提交模式）→ 跳过镜像")
        _write_gitignore()
        return
    added = updated = 0
    bytes_new = 0
    for top in sorted(os.listdir(AS)):
        if top in SKIP_TOP:
            continue
        s = os.path.join(AS, top)
        if not os.path.isdir(s):
            # 顶层文件（*.json / *.js / manifest.json）
            dst = os.path.join(WORK, top)
            if _copy_if_diff(s, dst):
                added += 1
                bytes_new += os.path.getsize(s)
            continue
        d = os.path.join(WORK, top)
        os.makedirs(d, exist_ok=True)
        for f in sorted(os.listdir(s)):
            if f in SKIP_TOP or f.endswith(".tmp"):
                continue
            sf = os.path.join(s, f)
            if not os.path.isfile(sf):
                continue
            df = os.path.join(d, f)
            if _copy_if_diff(sf, df):
                updated += 1
                bytes_new += os.path.getsize(sf)
    log("  镜像：新增/更新 %d 个文件，%.1f MB" % (updated + added, bytes_new / 1048576.0))
    _write_gitignore()


def _write_gitignore():
    """写本仓的 .gitignore（幂等）"""
    gi = os.path.join(WORK, ".gitignore")
    try:
        cur = open(gi, encoding="utf-8").read() if os.path.exists(gi) else ""
        if cur != WORK_GITIGNORE:
            open(gi, "w", encoding="utf-8").write(WORK_GITIGNORE)
    except Exception:
        pass


def _copy_if_diff(src, dst):
    try:
        if os.path.exists(dst) and os.path.getsize(dst) == os.path.getsize(src):
            return False
    except OSError:
        pass
    shutil.copy2(src, dst)
    return True


def publish(yes=False):
    n = count_webp(AS)
    nw = count_webp(WORK)
    log("  源 data/assets：%d 张 webp；work：%d 张" % (n, nw))
    if nw < MIN_CV:
        log("!! 护栏触发：work 里只有 %d 张 webp（下限 %d）→ 拒绝发布（源目录可能不完整）"
            % (nw, MIN_CV))
        sys.exit(2)
    cv = 0
    p = os.path.join(WORK, "cv")
    if os.path.isdir(p):
        cv = sum(1 for f in os.listdir(p) if f.endswith(".webp"))
    if cv < MIN_CV:
        log("!! 护栏触发：cv 只有 %d 张（下限 %d）→ 拒绝发布" % (cv, MIN_CV))
        sys.exit(2)

    git("add", "-A", check=False)
    r = git("diff", "--cached", "--quiet", check=False, capture=True)
    if r.returncode == 0:
        log("  无变更，跳过提交")
        return
    msg = os.environ.get("ASSET_COMMIT_MSG") or (
        "assets: %d 张图 + 索引同步 %s" % (nw, time.strftime("%Y-%m-%d %H:%M", time.localtime())))
    if not yes:
        log("  dry-run：将提交 %r（加 --yes 真推）" % msg)
        return
    git("commit", "-q", "-m", msg, check=False)
    for i in range(1, 5):
        r = git("push", "-u", "origin", "main", check=False, capture=True)
        if r.returncode == 0:
            log("  已推送 dwg-assets@main ✅")
            return
        log("  push 被拒，rebase 重试 %d/4" % i)
        git("fetch", "--depth=1", "origin", "main", check=False)
        git("rebase", "--autostash", "origin/main", check=False)
    log("!! 推送失败")
    sys.exit(3)


def status():
    log("work 目录：%s（%s）" % (WORK, "仓库" if os.path.isdir(os.path.join(WORK, ".git")) else "不存在"))
    log("  data/assets：%d 张 webp" % count_webp(AS))
    if os.path.isdir(os.path.join(WORK, ".git")):
        log("  work：%d 张 webp" % count_webp(WORK))
        r = git("status", "--porcelain", check=False, capture=True)
        lines = [x for x in (r.stdout or "").splitlines()]
        log("  未提交变更：%d 项" % len(lines))
        for x in lines[:8]:
            log("    " + x)
    log("  远端：%s" % REPO_HTTPS)


def main():
    cmd = (sys.argv[1] if len(sys.argv) > 1 else "status").lower()
    yes = "--yes" in sys.argv
    if cmd == "pull-in":
        pull_in()
        status()
        return
    if not os.path.isdir(AS):
        log("找不到 %s —— 先 checkout 出 data/assets（或跑 pull-in 从分仓拉）" % AS)
        sys.exit(1)
    if cmd == "status":
        status()
    elif cmd == "sync":
        ensure_clone()
        status()
    elif cmd == "mirror":
        ensure_clone()
        mirror()
    elif cmd == "publish":
        ensure_clone()
        publish(yes)
    elif cmd == "all":
        ensure_clone()
        mirror()
        publish(yes)
        status()
    else:
        log(__doc__)
        sys.exit(1)


if __name__ == "__main__":
    main()
