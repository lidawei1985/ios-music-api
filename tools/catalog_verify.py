#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""曲库体检器 —— 发布前的机器闸门（不合格直接退非零，阻断入库）

为什么必须有：2026-10-05 曲库混入 31% 翻唱垃圾、2026-10-06 又发现线上 13 万首里
61% 是 pop=5~9 的有声书/播客/白噪音/素材库。人眼抽查看不出量级问题，机器能。

检查项：
  A 结构：manifest 与实际分片一致、id 唯一、必需字段齐全、索引行数对得上
  B 可播：不含 VIP/付费(fee∈{1,4})、不含无版权下架(nrc=1)、时长在 30s~15min
  C 干净：歌名/歌手不命中垃圾与非歌曲词表
  D 成色：热度分位达标（p50 ≥ MIN_P50、热门占比 ≥ MIN_HOT_PCT）——这条专防「垃圾回灌」
用法：python tools/catalog_verify.py [catalog 目录，默认 data/catalog]
"""
import json, os, re, sys, glob, collections

CAT = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "catalog")

SKIP_FEE = {1, 4}
MIN_DUR, MAX_DUR = 30000, 900000
MIN_P50_POP = 10          # 中位热度下限（防垃圾回灌；实测干净库 p50≈35~85）
MIN_HOT_PCT = 20          # 热门(pop≥40)占比下限 %
JUNK_RE = re.compile(
    r"翻唱|cover|伴奏|instrumental|dj版|抖音|铃声|纯音乐|钢琴版|吉他版|尤克里里|八音盒|"
    r"口琴|陶笛|葫芦丝|萨克斯|二胡|古筝|电子琴|哼唱|清唱|翻自|ktv|慢速|加速|降调|升调|"
    r"remix|八轨|和声版|消音|立体声环绕|睡眠|白噪音|胎教|助眠|asmr|钢琴曲|轻音乐|"
    r"audiobook|audio\s*book|bookstream|朗读|有声书|有声剧|播客|podcast|广播剧|"
    r"chapter\s*\d|kapitel\s*\d|teil\s*\d|episode\s*\d|电台剧|朗读版|"
    r"nature\s*sound|yoga|meditation|spa\s*music|white\s*noise|rain\s*sound|"
    r"素材|production\s*music|trailer\s*music|背景音乐|彩铃", re.I)

bad = []
warn = []


def check(ok, msg):
    (print if ok else (lambda m: (bad.append(m), print(m))[1]))(("  ✓ " if ok else "  ✗ ") + msg)


man = json.load(open(os.path.join(CAT, "manifest.json"), encoding="utf-8"))
print("体检曲库：%s" % CAT)
print("manifest：count=%s 分片=%s 更新时间=%s"
      % (man.get("count"), len(man.get("shards") or []), man.get("updated")))

# ---------- A 结构 ----------
arr, ids = [], set()
files = set(os.path.basename(p) for p in glob.glob(os.path.join(CAT, "shard-*.json")))
miss = [s["file"] for s in man.get("shards") or [] if s["file"] not in files]
check(not miss, "分片文件齐全（缺 %d 个）" % len(miss))
dupe_struct = 0
for s in man.get("shards") or []:
    chunk = json.load(open(os.path.join(CAT, s["file"]), encoding="utf-8"))
    if len(chunk) != s.get("count"):
        dupe_struct += 1
    arr += chunk
check(dupe_struct == 0, "每个分片曲目数与 manifest 声明一致")
check(len(arr) == (man.get("count") or 0),
      "分片合计 %d == manifest %s" % (len(arr), man.get("count")))
for x in arr:
    if x.get("i") in ids:
        bad.append("重复曲目 id %s" % x.get("i"))
    ids.add(x.get("i"))
check(len(ids) == len(arr), "曲目 id 唯一（%d 个）" % len(ids))

need = ("i", "n", "a", "p")
lack = sum(1 for x in arr if any(not x.get(k) for k in need))
check(lack == 0, "必需字段齐全（缺 %d 条）" % lack)

idx = man.get("idx") or {}
idx_files = idx.get("chunks") or []
idx_missing = [f for f in idx_files if not os.path.exists(os.path.join(CAT, f))]
check(not idx_missing, "搜索索引分块齐全（缺 %d 个）" % len(idx_missing))
lines = 0
for f in idx_files:
    with open(os.path.join(CAT, f), encoding="utf-8") as fh:
        lines += sum(1 for _ in fh)
check(lines == len(arr), "索引行数 %d == 曲目数 %d" % (lines, len(arr)))
dangling = 0
seps = 0
for f in idx_files[:1]:
    for ln in open(os.path.join(CAT, f), encoding="utf-8"):
        parts = ln.rstrip("\n").split(idx.get("sep") or "\u0001")
        if len(parts) < 6:
            seps += 1
        else:
            try:
                si = int(parts[3])
                if not (0 <= si < len(man.get("shards") or [])):
                    dangling += 1
            except Exception:
                dangling += 1
check(seps == 0 and dangling == 0, "索引首块格式正确（异常 %d 行）" % (seps + dangling))

# ---------- B 可播 ----------
vip = sum(1 for x in arr if x.get("f") in SKIP_FEE)
nrc = sum(1 for x in arr if x.get("nrc"))
durbad = sum(1 for x in arr if not (MIN_DUR <= (x.get("d") or 0) <= MAX_DUR))
check(vip == 0, "无 VIP/付费曲目（%d 条）" % vip)
check(nrc == 0, "无版权下架曲目（%d 条）" % nrc)
check(durbad == 0, "时长均在 30s~15min（越界 %d 条）" % durbad)

# ---------- C 干净 ----------
junk = [x for x in arr if JUNK_RE.search(x.get("n") or "") or JUNK_RE.search(x.get("a") or "")][:5]
check(not junk, "无翻唱/伴奏/有声书/白噪音等垃圾（命中 %d 条，例：%s）"
      % (len([x for x in arr if JUNK_RE.search(x.get("n") or "") or JUNK_RE.search(x.get("a") or "")]),
         "；".join("%s—%s" % (x["n"][:16], x["a"][:12]) for x in junk[:2])))

# ---------- D 成色 ----------
pops = sorted(x.get("pop") or 0 for x in arr)
if pops:
    q = lambda p: pops[min(len(pops) - 1, int(len(pops) * p))]
    p50, hot = q(.5), sum(1 for p in pops if p >= 40)
    hot_pct = 100.0 * hot / len(pops)
    print("  成色：p10=%d p25=%d p50=%d p75=%d p90=%d ｜ 热门(pop≥40) %d = %.1f%% ｜ 歌手 %d 位"
          % (q(.1), q(.25), p50, q(.75), q(.9), hot, hot_pct, len({x["a"] for x in arr})))
    check(p50 >= MIN_P50_POP, "中位热度 p50=%d ≥ %d" % (p50, MIN_P50_POP))
    check(hot_pct >= MIN_HOT_PCT, "热门占比 %.1f%% ≥ %d%%" % (hot_pct, MIN_HOT_PCT))
    if hot_pct < 50:
        warn.append("热门占比偏低（%.1f%%）：库偏长尾" % hot_pct)

print("\n结论：%s" % ("❌ 体检不通过，禁止发布（%d 项）" % len(bad) if bad else "✅ 体检通过"))
for w in warn:
    print("  ⚠ " + w)
sys.exit(1 if bad else 0)
