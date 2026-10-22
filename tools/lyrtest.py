# -*- coding: utf-8 -*-
"""用**真实榜单曲目**测 LRCLIB 命中率（不是曲库里那批"伪热门"）。

为什么必须这么测：
    lyrics.yml 首轮跑了 600 首，只命中 41 首（6.8%）。看着像"LRCLIB 不行"，
    其实是我的抽样错了 —— 曲库按 pop 排序的前 600 首是儿歌/催眠曲/地下说唱
    （pop 字段在这些分片里普遍虚高），LRCLIB 当然没收录。
    真正该量的是「用户点开榜单点进去的歌」有多少能拿到词。

用法：python prototype/test_lrclib_charts.py 60
"""
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
HERE = os.path.dirname(os.path.abspath(__file__))  # .../cloud/tools
UA = {"User-Agent": "DawgeMusic/1.0.0 (https://github.com/lidawei1985/ios-music-app)"}
OP = urllib.request.build_opener(urllib.request.ProxyHandler({}))
SUFFIX = re.compile(r"[\(（\[【].*?[\)）\]】]", re.S)


def norm(s):
    return re.sub(r"[\s\-_（）()\[\]【】·,.，。'\"!！?？~～&]", "", (s or "")).lower()


def fetch_charts():
    """CI 里直接读仓库自带的数据（checkout 就有），不走 api.github.com。"""
    p = os.path.join(os.path.dirname(HERE), "data", "charts.json")
    if not os.path.exists(p):
        sys.exit("缺 data/charts.json")
    return json.load(open(p, encoding="utf-8"))


def get(a, t, retries=1):
    url = "https://lrclib.net/api/get?" + urllib.parse.urlencode({"artist_name": a, "track_name": t})
    for att in range(retries + 1):
        try:
            with OP.open(urllib.request.Request(url, headers=UA), timeout=20) as r:
                return json.loads(r.read().decode()), None
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None, "404"
            if e.code == 429:
                time.sleep(min(float(e.headers.get("Retry-After") or 5), 8))
                continue
            if e.code >= 500 and att < retries:
                time.sleep(1.5)
                continue
            return None, "HTTP%d" % e.code
        except Exception as e:
            if att < retries:
                time.sleep(1.2)
                continue
            return None, type(e).__name__
    return None, "retry"


def main():
    ap_n = 80
    for i, a in enumerate(sys.argv):
        if a == "--n" and i + 1 < len(sys.argv):
            ap_n = int(sys.argv[i + 1])
    n = ap_n
    ch = fetch_charts()
    songs = []
    for lst in ch.get("lists", []):
        for x in (lst.get("songs") or [])[:40]:
            # 榜单字段是 title/singer/duration(秒)，不是曲库那套 t/s/d。
            # 按曲库字段名取会得到 0 首（2026-10-22 实测踩过，白跑一轮）。
            t = (x.get("title") or x.get("t") or "").strip()
            a = (x.get("singer") or x.get("s") or "").strip()
            if t and a:
                songs.append({"t": t, "a": a, "chart": lst.get("name") or "?"})
    # 去重后取前 n 首
    seen, uniq = set(), []
    for x in songs:
        k = norm(x["t"]) + "|" + norm(x["a"])
        if k in seen:
            continue
        seen.add(k)
        uniq.append(x)
    uniq = uniq[:n]
    print("榜单曲目去重后取 %d 首（共 %d 个榜单）\n" % (len(uniq), len(ch.get("lists", []))))

    ok, bad = [], []
    t0 = time.time()
    for i, x in enumerate(uniq, 1):
        t = SUFFIX.sub("", x["t"]).strip()
        rec, err = get(t, x["a"])
        rec2 = None
        if rec is None:
            rec2, err2 = get(x["t"], x["a"])        # 剥后缀没中就再试原名
            if rec2 is not None:
                rec, err = rec2, None
        if rec and (rec.get("syncedLyrics") or "").strip() and not rec.get("instrumental"):
            ok.append(x)
            print("  %2d/%d ✅ %-30s [%d 行]" % (i, len(uniq), ("%s - %s" % (x["a"], x["t"]))[:30],
                                                len(rec["syncedLyrics"].splitlines())))
        else:
            why = err or ("纯文本无时间轴" if rec else "无")
            bad.append((x, why))
            print("  %2d/%d —  %-30s %s" % (i, len(uniq), ("%s - %s" % (x["a"], x["t"]))[:30], why))
        time.sleep(1.05)
    el = time.time() - t0
    print("\n=== 真实榜单命中率 ===")
    print("带时间轴命中 %d/%d = **%.1f%%** ｜耗时 %.0fs ｜%.1f 首/分" % (
        len(ok), len(uniq), 100.0 * len(ok) / max(1, len(uniq)), el, len(uniq) / el * 60))
    if bad:
        print("\n未命中原因：")
        for k, v in Counter(re.sub(r"\d+", "N", b[1]) for b in bad).most_common():
            print("  %-14s %d" % (k, v))


if __name__ == "__main__":
    main()
