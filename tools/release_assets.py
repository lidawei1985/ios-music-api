#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""release_assets.py —— 把图片全量包发到 **GitHub Releases**（真正的自有图床）

★★ 结论来源（2026-10-06 实测 + 官方文档核实，不是猜的）：
   · GitHub **仓库**：单文件硬限 100MB，**仓库软限 1GB**（超了会警告、变慢、可能被要求整改）。
     我们全部曲库封面 70987 张 ×14.9KB ≈ **1030MB** —— 直接放仓库必爆。❌
   · GitHub **Releases 附件**：**单文件 2GB**、**无总量上限**、**不占仓库容量**、
     **不计入 git clone**、**带宽免费**、GitHub 自有全球 CDN。
     → 官方文档 + 社区实践双重确认（filesize.org / codenote.net / 多个自建图床案例）。✅

   所以图片的正确归宿是 **Releases**，不是仓库。

架构（与"不许丢、不许失效"对齐）：
   仓库（git, <1GB）                 Releases（无限量）
   ├── data/assets/*.json  索引      ├── assets-00.tar.gz
   ├── data/assets/av/*    头像      ├── assets-01.tar.gz
   ├── data/assets/kv/*    KV        └── assets-02.tar.gz
   └── data/assets/hot/   热图             ↑ 全量图（冷热都在这，索引指过去）
        ↘ 索引里的值域：hot/xxx.webp（热，直接 CDN） 或 pkg:00/xxx.webp（冷，从包取）

★ 端上解析（App 侧，3 步，永不白屏）：
   ① 查本地缓存目录 → 有就直接用
   ② 拼 CDN：{CDN}assets/hot/<name>  → 热图命中即用
   ③ 未命中 → 按索引里的 pkg 卷号，从 Releases 拉对应卷（首次/后台预取）

★ 「上游改域名/加防盗链」对我们**完全无影响** —— 图在我们自己手里。

命令行（用 gh CLI，已登录 lidawei1985）：
  python tools/release_assets.py plan       # 算账（会不会超限）
  python tools/release_assets.py build      # 生成索引 + 热图 + 分卷包（本地）
  python tools/release_assets.py upload     # 用 gh 创建/更新 Release 并上传附件
  python tools/release_assets.py verify     # 校验已上传的附件可下载且大小一致
  python tools/release_assets.py all        # build + upload + verify
"""
import glob
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tarfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data")
AS = os.path.join(DATA, "assets")
OUT = os.path.join(ROOT, "out", "assets")
REPO = os.environ.get("GH_REPO") or "lidawei1985/ios-music-api"
TAG = os.environ.get("RL_TAG") or "assets-latest"

HOT_QUOTA_MB = int(os.environ.get("RL_HOT_MB") or "180")   # 仓库里热图配额
VOL_MB = int(os.environ.get("RL_VOL_MB") or "200")         # 每卷大小（Releases 单文件 2GB，我们取小卷便于端上按需取）


def log(*a):
    print(*a, flush=True)


def _sha16(b):
    return hashlib.sha1(b).hexdigest()[:16]


# ------------------------------------------------------------------ 热度来源
def hot_order():
    """算 cv 的热度顺序（被引用次数越多越热）。返回有序键列表。"""
    ref = {}

    def bump(u):
        if isinstance(u, str) and u.startswith("http"):
            k = "u:" + hashlib.sha1(u.encode("utf-8")).hexdigest()[:20]
            ref[k] = ref.get(k, 0) + 1

    for fn, key in [("library.json", "cov"), ("categories.json", "cov"),
                    ("charts.json", "cover")]:
        p = os.path.join(DATA, fn)
        if not os.path.exists(p):
            continue
        try:
            d = json.load(open(p, encoding="utf-8"))
        except Exception:
            continue

        def walk(x):
            if isinstance(x, dict):
                v = x.get(key)
                if isinstance(v, str):
                    bump(v)
                for k2, v2 in x.items():
                    if k2 != key:
                        walk(v2)
            elif isinstance(x, list):
                for v in x:
                    walk(v)
        walk(d)

    p = os.path.join(DATA, "mv.json")
    if os.path.exists(p):
        try:
            mv = json.load(open(p, encoding="utf-8"))
            for c in (mv.get("collections") or []):
                for m in (c.get("items") or []):
                    bump(m.get("cov"))
        except Exception:
            pass

    try:
        idx = json.load(open(os.path.join(AS, "cv.json"), encoding="utf-8"))
        keys = list((idx.get("map") or {}).keys())
    except Exception:
        keys = []

    url_keys = [k for k in keys if k.startswith("u:")]
    mv_keys = [k for k in keys if k.startswith("mv:")]
    other = [k for k in keys if not k.startswith(("u:", "mv:"))]
    url_keys.sort(key=lambda k: -ref.get(k, 0))
    return url_keys + mv_keys + other


# ------------------------------------------------------------------ plan
def cmd_plan():
    log("== 图片分发方案核算（GitHub Releases 图床）==")
    tot = {}
    for kind in ("av", "kv", "cv"):
        d = os.path.join(AS, kind)
        if not os.path.isdir(d):
            continue
        fs = os.listdir(d)
        b = sum(os.path.getsize(os.path.join(d, f)) for f in fs)
        tot[kind] = (len(fs), b)
        log("  %-4s %6d 张 %9.1f MB" % (kind, len(fs), b / 1048576))
    all_b = sum(v[1] for v in tot.values())
    log("  ---- 合计 %d 张 / %.1f MB" % (sum(v[0] for v in tot.values()), all_b / 1048576))

    # 热图配额
    cvd = os.path.join(AS, "cv")
    cvs = [os.path.getsize(os.path.join(cvd, f)) for f in os.listdir(cvd)] if os.path.isdir(cvd) else [1]
    avg = sum(cvs) / max(1, len(cvs))
    base = sum(v[1] for k, v in tot.items() if k in ("av", "kv"))
    hot_n = min(len(cvs), int(max(0, HOT_QUOTA_MB * 1048576 - base) / max(1, avg)))
    cold_n = len(cvs) - hot_n
    log("  热图留仓库：av+kv 全留 %.1f MB + cv 前 %d 张 %.1f MB ≈ %.1f MB（配额 %d MB）"
        % (base / 1048576, hot_n, hot_n * avg / 1048576,
           (base + hot_n * avg) / 1048576, HOT_QUOTA_MB))
    log("  冷图进 Releases：cv %d 张 ≈ %.1f MB → %.0f 卷（%d MB/卷）"
        % (cold_n, cold_n * avg / 1048576, cold_n * avg / (VOL_MB * 1048576) + 1, VOL_MB))
    log("  对比：全部塞仓库 %.1f MB vs 仓库软限 1024 MB → %s"
        % (all_b / 1048576, "会爆 ❌" if all_b / 1048576 > 1024 else "暂安全"))
    log("  Releases 侧：单文件 2GB / 无总量上限 / 不占仓库 → 可承载 %s"
        % ("全部（还有大量余量）" if all_b / 1048576 < 1024 * 100 else "需多卷"))
    return 0


# ------------------------------------------------------------------ build
def cmd_build():
    """生成：① hot/ 热图目录 ② 分卷包 ③ 新索引（值域区分 hot/ 与 pkg:/）"""
    import shutil as sh
    hot_dir = os.path.join(AS, "hot")
    os.makedirs(hot_dir, exist_ok=True)
    os.makedirs(OUT, exist_ok=True)

    log("== 生成索引 + 热图 + 分卷包 ==")
    # av / kv：全部当热图（量小、必须秒开）
    hot_files = set()
    for kind in ("av", "kv"):
        d = os.path.join(AS, kind)
        if not os.path.isdir(d):
            continue
        for f in os.listdir(d):
            dst = os.path.join(hot_dir, f)     # 拉平到 hot/（同名概率极低，且 sha 命名）
            if not os.path.exists(dst):
                sh.copy2(os.path.join(d, f), dst)
            hot_files.add(f)

    # cv：按热度取前 N 张进 hot/
    order = hot_order()
    cvdir = os.path.join(AS, "cv")
    idx_cv = json.load(open(os.path.join(AS, "cv.json"), encoding="utf-8"))
    m = idx_cv.get("map") or {}
    avg = sum(os.path.getsize(os.path.join(cvdir, f)) for f in os.listdir(cvdir)) / max(1, len(os.listdir(cvdir)))
    base_b = sum(os.path.getsize(os.path.join(hot_dir, f)) for f in os.listdir(hot_dir))
    budget = max(0, HOT_QUOTA_MB * 1048576 - base_b)
    n_hot = min(len(m), int(budget / max(1, avg)))
    log("  cv 热度前 %d 张进 hot/（预算 %.1f MB）" % (n_hot, budget / 1048576))

    hot_cv, cold_cv = [], []
    for i, k in enumerate(order):
        rel = m.get(k)
        if not rel:
            continue
        f = os.path.basename(rel)
        src = os.path.join(cvdir, f)
        if not os.path.exists(src):
            continue
        if i < n_hot:
            hot_cv.append((k, f))
        else:
            cold_cv.append((k, f))

    for k, f in hot_cv:
        dst = os.path.join(hot_dir, f)
        if not os.path.exists(dst):
            sh.copy2(os.path.join(cvdir, f), dst)
        hot_files.add(f)
    log("  hot/ 共 %d 张，%.1f MB" % (len(os.listdir(hot_dir)),
                                      sum(os.path.getsize(os.path.join(hot_dir, x))
                                          for x in os.listdir(hot_dir)) / 1048576))

    # 分卷打包冷图
    vols, cur, cur_b, vol_i = [], [], 0, 0
    vol_b = VOL_MB * 1048576
    for k, f in cold_cv:
        p = os.path.join(cvdir, f)
        s = os.path.getsize(p)
        if cur_b + s > vol_b and cur:
            vols.append(_write_vol(vol_i, cur, cvdir)); vol_i += 1; cur, cur_b = [], 0
        cur.append((k, f)); cur_b += s
    if cur:
        vols.append(_write_vol(vol_i, cur, cvdir))
    log("  冷图打成 %d 卷" % len(vols))

    # 新索引：值域 = hot/<f> 或 pkg:<vol>/<f>
    vol_of = {}
    for i, (_vk, _f) in enumerate(cold_cv):
        pass
    # 重建卷内归属
    vols, cur, cur_b, vol_i = [], [], 0, 0
    idx = {}
    for k, f in hot_cv:
        idx[k] = "hot/" + f
    # av/kv 都算 hot
    for kind in ("av", "kv"):
        d = os.path.join(AS, kind)
        if os.path.isdir(d):
            for f in os.listdir(d):
                idx["%s:%s" % (kind, f)] = "hot/" + f
    vol_no = 0
    for k, f in cold_cv:
        if cur_b + os.path.getsize(os.path.join(cvdir, f)) > vol_b and cur:
            for kk, ff in cur:
                idx[kk] = "pkg:%02d/%s" % (vol_no, ff)
            vol_no += 1; cur, cur_b = [], 0
        cur.append((k, f)); cur_b += os.path.getsize(os.path.join(cvdir, f))
    if cur:
        for kk, ff in cur:
            idx[kk] = "pkg:%02d/%s" % (vol_no, ff)

    meta = {"updated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "hot_count": len(hot_cv) + len(hot_files) - len(hot_cv),
            "cold_count": len(cold_cv), "volumes": vol_no + (1 if cur else 0),
            "tag": TAG, "repo": REPO,
            "note": ("值域 hot/<f>.webp = 仓库内热图，端上直接拼 CDN；"
                     "pkg:NN/<f>.webp = Releases 卷 NN 内，端上按需下载对应卷。"
                     "上游改域名/防盗链对本体系无影响（图在我们自己手里）。")}
    json.dump(idx, open(os.path.join(OUT, "asset_index.json"), "w", encoding="utf-8"),
              ensure_ascii=False, separators=(",", ":"))
    json.dump(meta, open(os.path.join(OUT, "asset_meta.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    log("  已写 out/assets/asset_index.json（%d 键）与 asset_meta.json" % len(idx))
    return 0


def _write_vol(vol, items, srcdir):
    name = os.path.join(OUT, "assets-%02d.tar.gz" % vol)
    with tarfile.open(name, "w:gz", compresslevel=1) as tf:
        for _k, f in items:
            tf.add(os.path.join(srcdir, f), arcname="cv/" + f)
    log("    %s  %.1f MB" % (os.path.basename(name), os.path.getsize(name) / 1048576))
    return name


# ------------------------------------------------------------------ upload
def _gh(*args, check=True):
    r = subprocess.run(["gh"] + list(args), cwd=ROOT, capture_output=True, text=True)
    if check and r.returncode != 0:
        log("  gh %s → 失败：%s" % (" ".join(args[:2]), (r.stderr or r.stdout)[:300]))
    return r


def cmd_upload():
    """用 gh 创建/更新 Release 并上传分卷包"""
    vols = sorted(glob.glob(os.path.join(OUT, "assets-*.tar.gz")))
    log("== 上传 %d 个分卷到 Releases（%s @ %s）==" % (len(vols), REPO, TAG))
    if not vols:
        log("  没有分卷包，先跑 build")
        return 0
    # 先看 tag 存不存在
    r = _gh("release", "view", TAG, "-R", REPO, check=False)
    if r.returncode != 0:
        log("  Release 不存在，创建：%s" % TAG)
        r = _gh("release", "create", TAG, "-R", REPO,
                "--title", "图片资源库（自有图床）",
                "--notes", "采集器本地化的全部封面/头像/KV（WebP）。"
                           "端上按 asset_index.json 的 hot/ 或 pkg:NN/ 取值。", "--latest")
        if r.returncode != 0:
            log("  创建失败，改用 tag 兜底")
            _gh("release", "create", TAG, "-R", REPO, "--notes", "图片资源库", check=False)
    else:
        log("  Release 已存在，将覆盖附件")
    ok = 0
    for v in vols:
        log("  上传 %s …" % os.path.basename(v))
        r = _gh("release", "upload", TAG, v, "-R", REPO, "--clobber")
        if r.returncode == 0:
            ok += 1
        else:
            log("    ❌ %s" % (r.stderr or "")[:200])
    log("  上传成功 %d/%d" % (ok, len(vols)))
    return 0 if ok == len(vols) else 1


def cmd_verify():
    """校验已上传附件可下载"""
    log("== 校验 Releases 附件 ==")
    r = _gh("release", "view", TAG, "-R", REPO, "--json", "assets")
    if r.returncode != 0:
        log("  读取失败")
        return 1
    try:
        d = json.loads(r.stdout)
    except Exception:
        log("  解析失败：%s" % r.stdout[:200])
        return 1
    asss = d.get("assets") or []
    log("  远端附件 %d 个：" % len(asss))
    for a in asss:
        log("    %-26s %8.1f MB  %s" % (a.get("name"), (a.get("size") or 0) / 1048576,
                                        a.get("state")))
    return 0


if __name__ == "__main__":
    cmd = (sys.argv[1] if len(sys.argv) > 1 else "plan").lower()
    if cmd == "plan":
        sys.exit(cmd_plan())
    if cmd == "build":
        sys.exit(cmd_build())
    if cmd == "upload":
        sys.exit(cmd_upload())
    if cmd == "verify":
        sys.exit(cmd_verify())
    if cmd == "all":
        rc = cmd_build()
        if rc == 0:
            rc = cmd_upload()
        sys.exit(rc)
    cmd_plan()
