#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""尾巴成色鉴定 + 扩容上限测算

要回答两个问题：
  1) 线上 13 万里那 8 万首 pop=5~9 的，到底是「冷门真歌」还是「垃圾」？（抽样看名字）
  2) 在只挡垃圾/不可播、不挡冷门的前提下，真实可达上限是多少？（多档位测算）
只读，不写产物。
"""
import json, os, re, random, collections

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SONGS_FILE = os.path.join(ROOT, "data", "catalog", "_songs.jsonl")
SKIP_FEE = {1, 4}
JUNK_RE = re.compile(
    r"翻唱|cover|伴奏|instrumental|dj版|抖音|铃声|纯音乐|钢琴版|吉他版|尤克里里|八音盒|"
    r"口琴|陶笛|葫芦丝|萨克斯|二胡|古筝|电子琴|哼唱|清唱|翻自|ktv|慢速|加速|降调|升调|"
    r"remix|八轨|和声版|消音|立体声环绕|睡眠|白噪音|胎教|助眠|asmr|钢琴曲|轻音乐", re.I)
MIN_DUR, MAX_DUR = 30000, 900000


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


def gate(songs, strong_n, pop_est, pop_new, name_max, art_max, min_art):
    strong_cnt, art_cnt = {}, {}
    for s in songs.values():
        if (s.get("pop") or 0) >= 30:
            strong_cnt[s["a"]] = strong_cnt.get(s["a"], 0) + 1
        art_cnt[s["a"]] = art_cnt.get(s["a"], 0) + 1
    strong = {a for a, c in strong_cnt.items() if c >= strong_n}
    out, why = {}, collections.Counter()
    for sid, s in songs.items():
        t, a = s.get("n") or "", s.get("a") or ""
        if JUNK_RE.search(t) or JUNK_RE.search(a):
            why["翻唱伴奏黑名单"] += 1; continue
        if len(t) > name_max or len(a) > art_max:
            why["名字异常"] += 1; continue
        if not (MIN_DUR <= (s.get("d") or 0) <= MAX_DUR):
            why["时长超限"] += 1; continue
        if art_cnt[a] < min_art:
            why["歌手候选<min"] += 1; continue
        if (s.get("pop") or 0) < (pop_est if a in strong else pop_new):
            why["热度不足"] += 1; continue
        out[sid] = s
    return out, strong, why


if __name__ == "__main__":
    songs = load()
    print("候选池（已剔 VIP/无版权/过短）:", len(songs), "首\n")

    print("=== 只挡垃圾/不可播，不挡冷门（热度地板=0）===")
    for label, kw in [
        ("原样全收（min_art=1）",        dict(strong_n=5, pop_est=0, pop_new=0, name_max=80, art_max=60, min_art=1)),
        ("要求歌手≥2 首候选",            dict(strong_n=5, pop_est=0, pop_new=0, name_max=80, art_max=60, min_art=2)),
        ("要求歌手≥3 首候选",            dict(strong_n=5, pop_est=0, pop_new=0, name_max=80, art_max=60, min_art=3)),
        ("要求歌手≥5 首候选",            dict(strong_n=5, pop_est=0, pop_new=0, name_max=80, art_max=60, min_art=5)),
        ("名/歌手 80-60 + 歌手≥2",       dict(strong_n=5, pop_est=0, pop_new=0, name_max=80, art_max=60, min_art=2)),
        ("名字放宽 120/80 + 歌手≥2",     dict(strong_n=5, pop_est=0, pop_new=0, name_max=120, art_max=80, min_art=2)),
    ]:
        out, strong, why = gate(songs, **kw)
        pops = sorted((x.get("pop") or 0) for x in out.values())
        q = lambda p: pops[min(len(pops) - 1, int(len(pops) * p))] if pops else 0
        hot = sum(1 for p in pops if p >= 40)
        print("  %-30s %7d 首 | 热度 p10=%3d p50=%3d | 热门(pop≥40) %6d | 剔:%s"
              % (label, len(out), q(.1), q(.5), hot,
                 "、".join("%s %d" % (k, v) for k, v in why.most_common(3))))

    print("\n=== 尾巴成色抽样（pop=5~9，随机 25 首，看是不是垃圾）===")
    out, _, _ = gate(songs, 5, 0, 0, 80, 60, 2)
    tail = [s for s in out.values() if (s.get("pop") or 0) < 10]
    random.seed(20261006)
    for s in random.sample(tail, 25):
        print("   pop=%-3d %-32s — %s" % (s.get("pop"), (s.get("n") or "")[:30], (s.get("a") or "")[:24]))

    print("\n=== 头部成色抽样（pop≥70，随机 15 首）===")
    head = [s for s in out.values() if (s.get("pop") or 0) >= 70]
    for s in random.sample(head, min(15, len(head))):
        print("   pop=%-3d %-32s — %s" % (s.get("pop"), (s.get("n") or "")[:30], (s.get("a") or "")[:24]))

    print("\n=== 歌手维度 ===")
    art_cnt = collections.Counter(s["a"] for s in out.values())
    print("  独立歌手 %d 位；只贡献 1 首的歌手 %d 位（占曲目 %d 首）"
          % (len(art_cnt), sum(1 for c in art_cnt.values() if c == 1),
             sum(c for c in art_cnt.values() if c == 1)))
    print("  曲目最多的歌手:", art_cnt.most_common(5))
