#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""验证「按播放量选歌单」能否显著提高真歌产出率（对比现有池）

假设（待验证）：现有池的垃圾来源是 enum 按「歌单曲目数」优先——有声书/素材库合辑曲目最多。
若改按「歌单播放量(playCount)」优先，热门歌单里应是真歌，pop 分布应显著上移。

只读探针，不写任何产物（结果只打印）。
用法：python tools/hotprobe.py [抽样歌单数，默认 150]
"""
import json, os, re, sys, time, ssl, urllib.request, urllib.parse, collections
from concurrent.futures import ThreadPoolExecutor, as_completed

CTX = ssl.create_default_context(); CTX.check_hostname = False; CTX.verify_mode = ssl.CERT_NONE
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
HDR = {"User-Agent": UA, "Referer": "https://music.163.com/",
       "Accept": "application/json, text/plain, */*", "Accept-Language": "zh-CN,zh;q=0.9"}
N_PL = int(sys.argv[1]) if len(sys.argv) > 1 else 150

JUNK_RE = re.compile(
    r"翻唱|cover|伴奏|instrumental|dj版|抖音|铃声|纯音乐|钢琴版|吉他版|尤克里里|八音盒|"
    r"口琴|陶笛|葫芦丝|萨克斯|二胡|古筝|电子琴|哼唱|清唱|翻自|ktv|慢速|加速|降调|升调|"
    r"remix|八轨|和声版|消音|立体声环绕|睡眠|白噪音|胎教|助眠|asmr|钢琴曲|轻音乐|"
    r"audiobook|bookstream|朗读|有声书|播客|podcast|广播剧|chapter\s*\d|teil\s*\d|"
    r"nature\s*sound|yoga|meditation|spa\s*music|white\s*noise|素材|production\s*music", re.I)


def get(url, data=None, timeout=25, retry=2):
    for i in range(retry):
        try:
            h = HDR if data is None else {**HDR, "Content-Type": "application/x-www-form-urlencoded"}
            req = urllib.request.Request(url, data=data, headers=h)
            with urllib.request.urlopen(req, timeout=timeout, context=CTX) as r:
                return json.loads(r.read().decode("utf-8", "replace"))
        except Exception:
            if i == retry - 1:
                return None
            time.sleep(0.5)
    return None


def enum_hot():
    """全部 + 华语/流行/欧美 等热门分类，按 playCount 优先"""
    cats = ["全部", "华语", "流行", "欧美", "民谣", "电子", "轻音乐", "摇滚", "说唱", "古风"]
    jobs = [(c, "hot", off) for c in cats for off in range(0, 300, 60)]
    pls, seen = [], set()
    with ThreadPoolExecutor(max_workers=8) as ex:
        for fut in as_completed([ex.submit(
                lambda j: get("https://music.163.com/api/playlist/list?cat=%s&order=%s&limit=60&offset=%d"
                              % (urllib.parse.quote(j[0]), j[1], j[2])), j) for j in jobs]):
            d = fut.result() or {}
            for p in (d.get("playlists") or []):
                pid = p.get("id")
                if pid and pid not in seen:
                    seen.add(pid)
                    pls.append({"id": pid, "n": p.get("trackCount") or 0,
                                "play": p.get("playCount") or 0, "name": (p.get("name") or "")[:40]})
    pls.sort(key=lambda x: -x["play"])          # ★ 关键差异：按播放量优先
    print("热门歌单池 %d 个（去重后），按播放量排序前 5: %s"
          % (len(pls), [(p["name"], p["n"], p["play"]) for p in pls[:5]]), flush=True)
    return pls[:N_PL]


def ids_of(pid):
    d = get("https://music.163.com/api/v6/playlist/detail?id=%d&n=1000&s=8" % pid, timeout=30)
    p = (d or {}).get("playlist") or {}
    return [int(t["id"] if isinstance(t, dict) else t) for t in (p.get("trackIds") or [])]


def songs_of(ids):
    body = "c=" + urllib.parse.quote(json.dumps([{"id": i} for i in ids], separators=(",", ":")))
    d = get("https://music.163.com/api/v3/song/detail", data=body.encode(), timeout=60)
    return (d or {}).get("songs") or []


if __name__ == "__main__":
    t0 = time.time()
    pls = enum_hot()
    ids, seen = [], set()
    with ThreadPoolExecutor(max_workers=8) as ex:
        for fut in as_completed([ex.submit(ids_of, p["id"]) for p in pls]):
            for i in fut.result():
                if i not in seen:
                    seen.add(i); ids.append(i)
    print("抽样 %d 个热门歌单 → 唯一曲目 id %d（%.0fs）" % (len(pls), len(ids), time.time() - t0), flush=True)

    songs = {}
    B = 1000
    with ThreadPoolExecutor(max_workers=6) as ex:
        futs = [ex.submit(songs_of, ids[i:i + B]) for i in range(0, len(ids), B)]
        for fut in as_completed(futs):
            for t in fut.result():
                sid = t.get("id")
                if not sid:
                    continue
                pop = int(t.get("pop") or 0)
                if sid not in songs or pop > songs[sid]["pop"]:
                    songs[sid] = {"id": sid, "n": t.get("name") or "", "pop": pop,
                                  "a": "/".join(a.get("name", "") for a in (t.get("ar") or [])),
                                  "f": t.get("fee") if t.get("fee") is not None else -1,
                                  "d": t.get("dt") or 0}
    print("取回元数据 %d 首（%.0fs）\n" % (len(songs), time.time() - t0), flush=True)

    def report(name, arr):
        if not arr:
            print(name, "空"); return
        pops = sorted(x["pop"] for x in arr)
        q = lambda p: pops[min(len(pops) - 1, int(len(pops) * p))]
        print("%-28s %6d 首 | p10=%3d p25=%3d p50=%3d p75=%3d p90=%3d | 热度≥40 %6d (%.0f%%) | ≥10 %6d (%.0f%%)"
              % (name, len(arr), q(.1), q(.25), q(.5), q(.75), q(.9),
                 sum(1 for p in pops if p >= 40), 100.0 * sum(1 for p in pops if p >= 40) / len(arr),
                 sum(1 for p in pops if p >= 10), 100.0 * sum(1 for p in pops if p >= 10) / len(arr)))

    all_s = list(songs.values())
    playable = [s for s in all_s if s["f"] not in (1, 4) and s["d"] >= 30000]
    clean = [s for s in playable if not JUNK_RE.search(s["n"]) and not JUNK_RE.search(s["a"])]
    report("热门歌单 全部", all_s)
    report("热门歌单 可播(非VIP)", playable)
    report("热门歌单 可播且非垃圾", clean)
    print("\n参考：现有池 24.1 万候选 → 热度≥40 只有 24,202 (10%)，≥10 只有 31,335 (13%)")
    print("JUNK 命中率：热门池 %.1f%% vs 现有池（地板10档）JUNK2 命中 31,563/241,411 = 13.1%%"
          % (100.0 * sum(1 for s in playable if JUNK_RE.search(s["n"]) or JUNK_RE.search(s["a"])) / max(1, len(playable))))
    random_sample = sorted(clean, key=lambda x: -x["pop"])[:15]
    print("\n热门池非垃圾 头部抽样:")
    for s in random_sample:
        print("   pop=%-3d %-34s — %s" % (s["pop"], s["n"][:32], s["a"][:24]))
    print("\n总耗时 %.0fs" % (time.time() - t0))
