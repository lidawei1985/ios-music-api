#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""时效锚点 (tide anchor)：把「某个 commit sha」写成一个 **10 分钟窗口命名**的 .js。

=======================================================================
为什么需要它（2026-10-06 判决性实测定案）
=======================================================================
jsDelivr 是「**按 URL** 缓存」，不是「按分支」：

  已被缓存过的 URL（@main / @latest 这类）→ 受硬缓存约束
      分支 12 小时 / 别名 7 天；purge 打不动，挂 ?t= 也打不动（实测纹丝不动）。
  **全新的 URL**（新文件名 / 新 commit sha / 新 tag）→ **立即回源**（实测 7 秒）。

判决性实测（真值取 api.github.com，零缓存；同一时刻并发对比）：
    @main/data/assets/cv.json         → count=17117（旧：卡满 12 小时）
    @<commit-sha>/data/assets/cv.json → count=23117（新：**立即**拿到）
    data/probe.json（全新文件名）      → 推完 7 秒即取到（**立即**）

于是端上要「推完数据手机立刻看到」，就必须先拿到一个**刚推上去的 sha**；
可拿 sha 本身又需要一个能秒到的 URL —— 用**每 10 分钟换一次的窗口文件名**破局：
    data/win/202610070000.js  →  window.__DWG_TIDE={"sha":"<sha>",...}
这个 URL 天生是新出现的，所以 jsDelivr 必然回源、秒级可得。
端上 probeTide() 按当前 UTC 时间向前最多追 12 个窗口（2 小时），
探到 sha 后把「索引 / 数据」整体切到 @<sha>（全新 URL → 秒级）；
**图片仍走 @main**（URL 稳定 → 客户端可长期缓存，不因换 sha 全部重下）。
探不到就回落 @main：慢 12 小时，但功能完全不受影响（原则：宁可慢，不能断）。

=======================================================================
为什么必须「两段提交」（CI 里的用法）
=======================================================================
锚点里要写的是**数据提交的 sha**，而 sha 只有提交完才知道 —— 先有鸡先有蛋。
所以 CI 里是两段：
    ① git add -A data && git commit && git push     → git rev-parse HEAD 得 sha A
    ② python tools/mktide.py --sha <sha A> --prune  → git commit && git push
端上探到锚点里的 sha A 后，用 @<sha A> 拉数据 —— 而 sha A 正是带着新数据的那个提交。

=======================================================================
用法
=======================================================================
    python tools/mktide.py                      # 用 git HEAD 的 sha 写当前窗口
    python tools/mktide.py --sha abc1234        # 显式指定 sha
    python tools/mktide.py --prune              # 只保留最近 KEEP 个窗口（清旧锚点）
    python tools/mktide.py --dir kernel/win     # 换个目录（ios 仓内核锚点用）
    python tools/mktide.py --back 2             # 连写「当前 + 前 2 个窗口」（防跨窗抖动）
"""
import argparse, json, os, subprocess, sys, time

CD = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
KEEP = 48            # 保留最近 48 个窗口 = 8 小时（端上最多追 2 小时，余量充足）


def win_name(dt=None):
    """10 分钟粒度的窗口名：YYYYMMDDHHM0（UTC）。与端上 probeTide() 严格一致。"""
    t = dt or time.gmtime()
    return "%04d%02d%02d%02d%1d0" % (t.tm_year, t.tm_mon, t.tm_mday,
                                     t.tm_hour, t.tm_min // 10)


def git_head_sha():
    try:
        return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=CD,
                                       text=True, stderr=subprocess.DEVNULL).strip()
    except Exception as e:
        print("[mktide] 取 git HEAD 失败：%s" % e, file=sys.stderr)
        return ""


def write_one(outdir, win, payload):
    dst = os.path.join(outdir, win + ".js")
    tmp = dst + ".tmp"
    body = "window.__DWG_TIDE=%s;" % json.dumps(payload, ensure_ascii=False,
                                                separators=(",", ":"))
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(body)
    os.replace(tmp, dst)      # 原子替换：端上绝不会读到半个文件
    return dst, len(body.encode("utf-8"))


def prune(outdir, keep=KEEP):
    """只保留最近 keep 个窗口，删掉更早的（否则每 10 分钟一个文件，仓会慢慢肿）。"""
    if not os.path.isdir(outdir):
        return 0
    wins = sorted([f[:-3] for f in os.listdir(outdir)
                   if f.endswith(".js") and len(f) == 15 and f[:-3].isdigit()])
    n = 0
    for w in wins[:-keep] if len(wins) > keep else []:
        try:
            os.remove(os.path.join(outdir, w + ".js"))
            n += 1
        except Exception:
            pass
    return n


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sha", default="")
    ap.add_argument("--dir", default="data/win")
    ap.add_argument("--round", default="")
    ap.add_argument("--prune", action="store_true")
    ap.add_argument("--back", type=int, default=1,
                    help="连写当前 + 前 N-1 个窗口（默认 1，即只写当前）")
    a = ap.parse_args()

    outdir = a.dir if os.path.isabs(a.dir) else os.path.join(CD, a.dir)
    os.makedirs(outdir, exist_ok=True)

    sha = a.sha.strip() or git_head_sha()
    if len(sha) < 7:
        print("[mktide] 没有可用 sha，放弃（不影响其它流程）", file=sys.stderr)
        return 1

    now = time.time()
    wrote = []
    for i in range(max(1, a.back)):
        t = time.gmtime(now - i * 600)
        payload = {"sha": sha, "t": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now))}
        if a.round:
            payload["round"] = a.round
        dst, sz = write_one(outdir, win_name(t), payload)
        wrote.append((os.path.basename(dst), sz))

    removed = prune(outdir) if a.prune else 0
    for n, sz in wrote:
        print("  %s  %d B" % (n, sz))
    print("[mktide] 锚点 sha=%s  写入 %d 个%s"
          % (sha[:12], len(wrote), ("  清理 %d 个旧窗口" % removed) if removed else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
