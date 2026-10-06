#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""曲库闸门调参分析：不同阈值下能留下多少首、热度分布如何

用途：决定「26 万扩容」应有的质量地板（数据驱动，不拍脑袋）。
只读 _songs.jsonl，不写任何产物。
"""
import json, os, re, sys, time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SONGS_FILE = os.path.join(ROOT, "data", "catalog", "_songs.jsonl")

SKIP_FEE = {1, 4}
JUNK_RE = re.compile(
    r"翻唱|cover|伴奏|instrumental|dj版|抖音|铃声|纯音乐|钢琴版|吉他版|尤克里里|八音盒|"
    r"口琴|陶笛|葫芦丝|萨克斯|二胡|古筝|电子琴|哼唱|清唱|翻自|ktv|慢速|加速|降调|升调|"
    r"remix|八轨|和声版|消音|立体声环绕|睡眠|白噪音|胎教|助眠|asmr|钢琴曲|轻音乐", re.I)
MIN_DUR, MAX_DUR = 30000, 900000


def load():
    songs, drop = {}, {}
    for line in open(SONGS_FILE, encoding="utf-8"):
        try:
            rec = json.loads(line)
        except Exception:
            continue
        for s in rec.get("songs") or []:
            sid = s.get("i")
            if not sid or not s.get("n") or not s.get("a") or not s.get("p"):
                drop["字段不全"] = drop.get("字段不全", 0) + 1
                continue
            if s.get("f") in SKIP_FEE:
                drop["付费不可播"] = drop.get("付费不可播", 0) + 1
                continue
            if s.get("nrc"):
                drop["无版权下架"] = drop.get("无版权下架", 0) + 1
                continue
            if (s.get("d") or 0) < MIN_DUR:
                drop["时长过短"] = drop.get("时长过短", 0) + 1
                continue
            if sid not in songs or (s.get("pop") or 0) > (songs[sid].get("pop") or 0):
                songs[sid] = s
    return songs, drop


def gate(songs, strong_n, pop_est, pop_new, name_max, art_max, min_art_songs=1):
    """返回通过闸门的曲目；min_art_songs = 该歌手至少要有多首候选（防一次性灌水）"""
    strong_cnt, art_cnt = {}, {}
    for s in songs.values():
        if (s.get("pop") or 0) >= 30:
            strong_cnt[s["a"]] = strong_cnt.get(s["a"], 0) + 1
        art_cnt[s["a"]] = art_cnt.get(s["a"], 0) + 1
    strong = {a for a, c in strong_cnt.items() if c >= strong_n}
    out, why = {}, {}
    for sid, s in songs.items():
        t, a = s.get("n") or "", s.get("a") or ""
        if JUNK_RE.search(t) or JUNK_RE.search(a):
            why["翻唱伴奏"] = why.get("翻唱伴奏", 0) + 1; continue
        if len(t) > name_max or len(a) > art_max:
            why["名字异常"] = why.get("名字异常", 0) + 1; continue
        if not (MIN_DUR <= (s.get("d") or 0) <= MAX_DUR):
            why["时长超限"] = why.get("时长超限", 0) + 1; continue
        if art_cnt[a] < min_art_songs:
            why["歌手单曲灌水"] = why.get("歌手单曲灌水", 0) + 1; continue
        floor = pop_est if a in strong else pop_new
        if (s.get("pop") or 0) < floor:
            why["热度不足"] = why.get("热度不足", 0) + 1; continue
        out[sid] = s
    return out, strong, why


def hist(songs, label):
    pops = sorted((s.get("pop") or 0) for s in songs.values())
    if not pops:
        print(label, "空"); return
    q = lambda p: pops[min(len(pops) - 1, int(len(pops) * p))]
    print("%-46s 共 %6d 首 | p10=%3d p25=%3d p50=%3d p75=%3d p90=%3d"
          % (label, len(pops), q(.10), q(.25), q(.50), q(.75), q(.90)))


if __name__ == "__main__":
    t0 = time.time()
    songs, drop = load()
    print("候选唯一曲目 %d ｜ 已剔除：%s  （%.0fs）"
          % (len(songs), "、".join("%s %d" % (k, v) for k, v in drop.items()), time.time() - t0))
    hist(songs, "全部候选（含未过闸门）")

    combos = [
        ("现状闸门 (strong>=5, est10/new40, 名字80/60)", dict(strong_n=5, pop_est=10, pop_new=40, name_max=80, art_max=60)),
        ("放宽 strong>=3",                              dict(strong_n=3, pop_est=10, pop_new=40, name_max=80, art_max=60)),
        ("放宽 new40->30",                              dict(strong_n=5, pop_est=10, pop_new=30, name_max=80, art_max=60)),
        ("strong>=3 + new30",                          dict(strong_n=3, pop_est=10, pop_new=30, name_max=80, art_max=60)),
        ("strong>=3 + new25",                          dict(strong_n=3, pop_est=10, pop_new=25, name_max=80, art_max=60)),
        ("strong>=3 + new20",                          dict(strong_n=3, pop_est=10, pop_new=20, name_max=80, art_max=60)),
        ("strong>=3 + new15",                          dict(strong_n=3, pop_est=10, pop_new=15, name_max=80, art_max=60)),
        ("strong>=3 + new10（=一视同仁，只留硬地板）",   dict(strong_n=3, pop_est=10, pop_new=10, name_max=80, art_max=60)),
        ("strong>=2 + new15 + 名字120/80",              dict(strong_n=2, pop_est=8,  pop_new=15, name_max=120, art_max=80)),
        ("strong>=2 + new10 + 名字120/80",              dict(strong_n=2, pop_est=8,  pop_new=10, name_max=120, art_max=80)),
    ]
    for label, kw in combos:
        out, strong, why = gate(songs, **kw)
        print("  %-44s -> %6d 首（实力歌手 %d 人）" % (label, len(out), len(strong)))
        if kw["pop_new"] >= 10:
            hist(out, "      " + label.split("(")[0])
    print("总耗时 %.0fs" % (time.time() - t0))
