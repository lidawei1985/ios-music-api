#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""垃圾二次识别（JUNK2）+ 干净真歌上限测算

背景（2026-10-06 实测）：
  线上 13 万曲库里 61% 是 pop=5~9，抽样全是「有声书 / 播客 / 助眠白噪音 / 素材库配乐 / 瑜伽音乐」，
  不是歌。主人原则：「质量和热度才是真的关键点」「新歌替换没有热度的烂歌」。
所以这里做两件事：
  1) JUNK2 = 原黑名单 + 有声书/播客/白噪音/助眠/瑜伽/素材库 特征；
  2) 在 JUNK2 下测算「干净真歌」到底有多少首（不同热度地板的对比），并抽样验证没误杀真歌。
只读，不写产物。
"""
import json, os, re, random, collections

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SONGS_FILE = os.path.join(ROOT, "data", "catalog", "_songs.jsonl")
SKIP_FEE = {1, 4}
MIN_DUR, MAX_DUR = 30000, 900000

JUNK_RE = re.compile(
    r"翻唱|cover|伴奏|instrumental|dj版|抖音|铃声|纯音乐|钢琴版|吉他版|尤克里里|八音盒|"
    r"口琴|陶笛|葫芦丝|萨克斯|二胡|古筝|电子琴|哼唱|清唱|翻自|ktv|慢速|加速|降调|升调|"
    r"remix|八轨|和声版|消音|立体声环绕|睡眠|白噪音|胎教|助眠|asmr|钢琴曲|轻音乐", re.I)

# 新增：非歌曲类内容（有声书/播客/朗读/电台剧/冥想瑜伽/素材库配乐）
JUNK2_RE = re.compile(
    r"audiobook|audio book|bookstream|朗读|有声书|有声剧|播客|podcast|广播剧|"
    r"chapter\s*\d|part\s*\d+\b|teil\s*\d|episode\s*\d|电台|朗读版|"
    r"nature\s*sound|yoga|meditation|relax(ing|ation)?\s*music|spa\s*music|massage|"
    r"white\s*noise|rain\s*sound|thunder|素材|production\s*music|trailer\s*music|"
    r"demo\s*tape|karaoke|背景音乐|BGM\b|铃声版|彩铃", re.I)


def load():
    songs = {}
    for line in open(SONGS_FILE, encoding="utf-8"):
        try:
            rec = json.loads(line)
        except Exception:
            continue
        for s in rec.get("songs") or []:
            sid = s.get("i")
            if not sid or not s.get("n") or not s.get("a") or not s.get("p"):
                continue
            if s.get("f") in SKIP_FEE or s.get("nrc"):
                continue
            if (s.get("d") or 0) < MIN_DUR:
                continue
            if sid not in songs or (s.get("pop") or 0) > (songs[sid].get("pop") or 0):
                songs[sid] = s
    return songs


def gate(songs, pop_floor, name_max, art_max, min_art, use_junk2=True):
    art_cnt = collections.Counter(s["a"] for s in songs.values())
    out, why = {}, collections.Counter()
    for sid, s in songs.items():
        t, a = s.get("n") or "", s.get("a") or ""
        if JUNK_RE.search(t) or JUNK_RE.search(a):
            why["翻唱伴奏"] += 1; continue
        if use_junk2 and (JUNK2_RE.search(t) or JUNK2_RE.search(a)):
            why["非歌曲内容(书/播客/白噪/素材)"] += 1; continue
        if len(t) > name_max or len(a) > art_max:
            why["名字异常"] += 1; continue
        if not (MIN_DUR <= (s.get("d") or 0) <= MAX_DUR):
            why["时长超限"] += 1; continue
        if art_cnt[a] < min_art:
            why["歌手候选<min"] += 1; continue
        if (s.get("pop") or 0) < pop_floor:
            why["热度不足"] += 1; continue
        out[sid] = s
    return out, why


def stat(out):
    pops = sorted((x.get("pop") or 0) for x in out.values())
    if not pops:
        return "空", 0
    q = lambda p: pops[min(len(pops) - 1, int(len(pops) * p))]
    return "p10=%3d p25=%3d p50=%3d p75=%3d p90=%3d | 热门(pop≥40) %6d (%.0f%%)" % (
        q(.1), q(.25), q(.5), q(.75), q(.9),
        sum(1 for p in pops if p >= 40), 100.0 * sum(1 for p in pops if p >= 40) / len(pops)), len(pops)


if __name__ == "__main__":
    songs = load()
    print("候选池（已剔 VIP/无版权/过短）: %d 首\n" % len(songs))

    print("=== JUNK2 各档位（名字 120/80，歌手≥2 首候选）===")
    for f in (0, 5, 10, 15, 20, 25, 30, 40):
        out, why = gate(songs, f, 120, 80, 2)
        s, n = stat(out)
        print("  热度地板≥%-3d -> %7d 首 | %s" % (f, n, s))
    out, why = gate(songs, 10, 120, 80, 2)
    print("  剔除明细(地板10):", dict(why.most_common()))

    print("\n=== 对比：不做 JUNK2（仅原黑名单）地板≥10 ===")
    out0, _ = gate(songs, 10, 120, 80, 2, use_junk2=False)
    s, n = stat(out0)
    print("  -> %d 首 | %s" % (n, s))

    print("\n=== 被 JUNK2 判为『非歌曲』的抽样 20 条（验证是否真垃圾）===")
    killed = [s for s in songs.values()
              if (JUNK2_RE.search(s.get("n") or "") or JUNK2_RE.search(s.get("a") or ""))
              and not (JUNK_RE.search(s.get("n") or "") or JUNK_RE.search(s.get("a") or ""))]
    random.seed(7)
    for s in random.sample(killed, min(20, len(killed))):
        print("   pop=%-3d %-40s — %s" % (s.get("pop"), (s.get("n") or "")[:38], (s.get("a") or "")[:26]))
    print("   JUNK2 命中总数:", len(killed))

    print("\n=== 地板≥10 保留库抽样 20 条（验证全是真歌）===")
    out, _ = gate(songs, 10, 120, 80, 2)
    arr = sorted(out.values(), key=lambda x: -(x.get("pop") or 0))
    for s in random.sample(arr, min(20, len(arr))):
        print("   pop=%-3d %-34s — %s" % (s.get("pop"), (s.get("n") or "")[:32], (s.get("a") or "")[:24]))

    print("\n=== 分层构成（地板≥10 保留库）===")
    for band, lo, hi in [("热门 pop≥70", 70, 101), ("热度 40-69", 40, 70),
                         ("热度 25-39", 25, 40), ("热度 10-24", 10, 25)]:
        print("  %-12s %6d 首" % (band, sum(1 for s in out.values() if lo <= (s.get("pop") or 0) < hi)))
