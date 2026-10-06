#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""pack_assets.py —— 图片沉淀的**分发打包器**（走 GitHub Releases，不占仓库）

★★ 为什么需要这个（主人点破「不要用 git」之后重做的方案）：
   GitHub **仓库**硬限 1GB，而我们全部曲库封面实测 **1030MB**（70987 张 ×14.9KB），
   一放进去就顶到头、push 被拒、整轮采集成果全丢。
   但 **GitHub Releases 附件单文件 2GB、无总量硬限** —— 这才是图片该走的路。

   于是分层：
     · **仓库（git）** 只放「索引 + 热门热图」：assets/*.json + av/kv + cv 里热度靠前的
       —— 体量控制在 ~200MB，保证 git push 永远成功、App 首屏图秒开。
     · **Releases** 放全量图包（分卷 tar，每卷 400MB）—— 端上按需下载/离线缓存。

   关键：**索引（assets/*.json）始终是单一真源**，值形如 `cv/abc.webp`。
   端上解析规则：
     ① 先查本地缓存目录  ② 未命中 → 拼 CDN 热图路径
     ③ 仍未命中 → 按索引从 Releases 取对应分卷（端上按需/后台预取）
   这样"上游改域名/防盗链"对我们**完全无影响** —— 图在我们自己手里。

用法：
  python tools/pack_assets.py plan     # 只算账，不打包（看会不会超限）
  python tools/pack_assets.py split    # 按热度切分：热图留仓库 / 其余入包
  python tools/pack_assets.py pack     # 打 Releases 分卷包到 out/
  python tools/pack_assets.py all      # split + pack
"""
import glob
import json
import os
import shutil
import sys
import tarfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data")
AS = os.path.join(DATA, "assets")
OUT = os.path.join(ROOT, "out", "assets")

# 仓库里能留的热图配额（MB）—— 与 asset_guard 的 ASSET_LIMIT_MB 呼应，留足余量
HOT_QUOTA_MB = int(os.environ.get("PK_HOT_QUOTA_MB") or "180")
# 每卷分卷包上限（MB）—— Releases 单文件 2GB，取 400MB 便于端上按卷取
VOL_MB = int(os.environ.get("PK_VOL_MB") or "400")


def log(*a):
    print(*a, flush=True)


def load_idx(kind):
    p = os.path.join(AS, kind + ".json")
    if not os.path.exists(p):
        return None
    try:
        return json.load(open(p, encoding="utf-8"))
    except Exception:
        return None


def hot_keys():
    """算「哪些图算热图」（留在仓库里）。

    ★ 热度来源按类不同：
      · av  → artists.json 的 seen（歌手出现次数）
      · kv  → 全部算热（首页就这 24 张，必须秒开）
      · cv  → 该图在 library/categories/charts/mv 里被引用的次数（引用越多越热）
    """
    hot = {"av": set(), "kv": set(), "cv": set()}

    # av：按 seen 排
    try:
        arts = json.load(open(os.path.join(DATA, "artists.json"), encoding="utf-8"))
        idx = load_idx("av") or {"map": {}}
        m = idx.get("map") or {}
        for a in sorted(arts.get("artists") or [], key=lambda x: -(x.get("seen") or 0)):
            n = (a.get("n") or "").strip()
            if n in m:
                hot["av"].add(n)
    except Exception:
        pass

    # kv：全留
    idx = load_idx("kv") or {"map": {}}
    hot["kv"] = set((idx.get("map") or {}).keys())

    # cv：按被引用次数排序
    import hashlib
    ref = {}

    def bump(url):
        if isinstance(url, str) and url.startswith("http"):
            ref["u:" + hashlib.sha1(url.encode("utf-8")).hexdigest()[:20]] = \
                ref.get("u:" + hashlib.sha1(url.encode("utf-8")).hexdigest()[:20], 0) + 1

    for fn, key in [("library.json", "cov"), ("categories.json", "cov"),
                    ("charts.json", "cover")]:
        try:
            d = json.load(open(os.path.join(DATA, fn), encoding="utf-8"))
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

    try:
        mv = json.load(open(os.path.join(DATA, "mv.json"), encoding="utf-8"))
        for c in (mv.get("collections") or []):
            for m in (c.get("items") or []):
                bump(m.get("cov"))
    except Exception:
        pass

    idx = load_idx("cv") or {"map": {}}
    m = idx.get("map") or {}
    order = sorted(m.keys(), key=lambda k: -ref.get(k, 0))
    hot["cv"] = list(order)

    return hot, ref


def cmd_plan():
    log("== 图片分发方案核算 ==")
    hot, ref = hot_keys()
    total_bytes = 0
    for kind in ("av", "kv", "cv"):
        d = os.path.join(AS, kind)
        if not os.path.isdir(d):
            continue
        n = len(os.listdir(d))
        b = sum(os.path.getsize(os.path.join(d, f)) for f in os.listdir(d))
        total_bytes += b
        log("  %-4s %6d 张  %8.1f MB" % (kind, n, b / 1048576))
    log("  ---- 合计 %.1f MB" % (total_bytes / 1048576))

    # 模拟：cv 留多少张能压在 HOT_QUOTA_MB 内
    cvd = os.path.join(AS, "cv")
    files = os.listdir(cvd)
    sizes = sorted((os.path.getsize(os.path.join(cvd, f)) for f in files), reverse=True)
    avg = sum(sizes) / max(1, len(sizes))
    other = 0
    for kind in ("av", "kv"):
        d = os.path.join(AS, kind)
        if os.path.isdir(d):
            other += sum(os.path.getsize(os.path.join(d, f)) for f in os.listdir(d))
    quota = HOT_QUOTA_MB * 1048576
    hot_n = int(max(0, (quota - other)) / max(1, avg))
    hot_n = min(hot_n, len(files))
    log("  热图配额 %d MB → av+kv %d 张占 %.1f MB，cv 可留约 %d 张（其余 %d 张入 Releases）"
        % (HOT_QUOTA_MB, len(hot.get("av", [])) + len(hot.get("kv", [])),
           other / 1048576, hot_n, len(files) - hot_n))
    pack_b = (len(files) - hot_n) * avg
    log("  Releases 全量包约 %.1f MB → 按 %d MB/卷 切 %d 卷"
        % (pack_b / 1048576, VOL_MB, int(pack_b / (VOL_MB * 1048576)) + 1))
    return 0


def cmd_split():
    """把不在热表里的图从仓库目录移出到 out/cold/（交给 Releases）"""
    hot, _ref = hot_keys()
    cold_dir = os.path.join(ROOT, "out", "cold")
    os.makedirs(cold_dir, exist_ok=True)
    moved = 0
    for kind in ("av", "kv", "cv"):
        d = os.path.join(AS, kind)
        if not os.path.isdir(d):
            continue
        idx = load_idx(kind) or {"map": {}}
        m = idx.get("map") or {}
        hot_set = set(hot.get(kind) or [])
        # 反查：文件 → 键
        hot_files = set()
        for k in hot_set:
            rel = m.get(k)
            if rel:
                hot_files.add(os.path.basename(rel))
        for f in os.listdir(d):
            if f in hot_files:
                continue
            src = os.path.join(d, f)
            dst = os.path.join(cold_dir, kind, f)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            if not os.path.exists(dst):
                shutil.move(src, dst)
                moved += 1
    log("已移出冷图 %d 张到 out/cold/（待打包）" % moved)
    return 0


def cmd_pack():
    """把 out/cold/ 打成 Releases 分卷包"""
    cold = os.path.join(ROOT, "out", "cold")
    if not os.path.isdir(cold):
        log("没有 out/cold/，先跑 split")
        return 1
    os.makedirs(OUT, exist_ok=True)
    files = []
    for kind in ("av", "kv", "cv"):
        d = os.path.join(cold, kind)
        if os.path.isdir(d):
            for f in sorted(os.listdir(d)):
                files.append((kind, f))
    log("待打包 %d 张" % len(files))

    vol = 0
    cur, cur_b, made = [], 0, []
    vol_b = VOL_MB * 1048576
    for kind, f in files:
        p = os.path.join(cold, kind, f)
        s = os.path.getsize(p)
        if cur_b + s > vol_b and cur:
            made.append(_write_vol(vol, cur, cold))
            vol += 1
            cur, cur_b = [], 0
        cur.append((kind, f))
        cur_b += s
    if cur:
        made.append(_write_vol(vol, cur, cold))
    log("已生成 %d 卷：" % len(made))
    for m in made:
        log("   %s  %.1f MB" % (os.path.basename(m), os.path.getsize(m) / 1048576))
    # 校验清单
    manifest = {"updated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "volumes": [os.path.basename(m) for m in made],
                "count": len(files), "vol_mb": VOL_MB,
                "note": "图片全量包（冷图），走 GitHub Releases 分发，不占仓库容量。"}
    json.dump(manifest, open(os.path.join(OUT, "manifest.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    log("已写 out/assets/manifest.json")
    return 0


def _write_vol(vol, items, cold):
    name = os.path.join(OUT, "assets-cold-%02d.tar.gz" % vol)
    with tarfile.open(name, "w:gz", compresslevel=1) as tf:
        for kind, f in items:
            tf.add(os.path.join(cold, kind, f), arcname="%s/%s" % (kind, f))
    return name


if __name__ == "__main__":
    cmd = (sys.argv[1] if len(sys.argv) > 1 else "plan").lower()
    if cmd == "plan":
        sys.exit(cmd_plan())
    if cmd == "split":
        sys.exit(cmd_split())
    if cmd == "pack":
        sys.exit(cmd_pack())
    if cmd == "all":
        cmd_split()
        sys.exit(cmd_pack())
    cmd_plan()
