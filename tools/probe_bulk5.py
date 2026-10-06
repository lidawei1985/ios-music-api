# -*- coding: utf-8 -*-
"""第五轮：歌单池只挖到 1470 个（8.5 万首），找增量源 —— 歌手 / 搜索翻页。"""
import json, urllib.request, urllib.parse, ssl, time

CTX = ssl.create_default_context(); CTX.check_hostname = False; CTX.verify_mode = ssl.CERT_NONE
HDR = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36",
       "Referer": "https://music.163.com/", "Accept": "application/json, text/plain, */*"}


def get(url, timeout=25):
    try:
        req = urllib.request.Request(url, headers=HDR)
        with urllib.request.urlopen(req, timeout=timeout, context=CTX) as r:
            return json.loads(r.read().decode("utf-8", "replace"))
    except Exception as e:
        return {"__err__": "%s: %s" % (type(e).__name__, e)}


print("=== ① 歌手列表（分页上限） ===")
for init in ["a", "z", "热"]:
    d = get("https://music.163.com/api/artist/list?type=1&area=7&initial=%s&limit=100&offset=0" % urllib.parse.quote(init))
    ar = d.get("artists") or []
    print("  initial=%-3s code=%-6s artists=%-4d total=%s  首=%s" % (
        init, d.get("code"), len(ar), d.get("total") or d.get("more"),
        [a.get("name") for a in ar[:3]]))
    time.sleep(0.3)

print("\n=== ② 歌手热门歌（单次多少） ===")
d = get("https://music.163.com/api/artist/list?type=1&area=7&initial=%s&limit=5&offset=0" % urllib.parse.quote("周"))
ar = (d.get("artists") or [])
cand = [(a["id"], a.get("name")) for a in ar] or [(6452, "周杰伦")]
for aid, nm in cand[:2]:
    d2 = get("https://music.163.com/api/artist/top/song?id=%d" % aid)
    print("  歌手 %s(%s): code=%s songs=%d" % (nm, aid, d2.get("code"), len(d2.get("songs") or [])))
    d3 = get("https://music.163.com/api/v1/artist/songs?id=%d&limit=500&offset=0&order=hot" % aid)
    print("     v1/artist/songs: code=%s songs=%d" % (d3.get("code"), len(d3.get("songs") or [])))
    time.sleep(0.3)

print("\n=== ③ 搜索接口翻页上限 ===")
for off in [0, 100, 500, 900, 1000, 1200]:
    d = get("https://music.163.com/api/search/get?s=%s&type=1&limit=100&offset=%d" % (urllib.parse.quote("爱"), off))
    r = (d.get("result") or {})
    print("  offset=%-5d code=%-6s songCount=%-7s 回包=%d" % (off, d.get("code"), r.get("songCount"), len(r.get("songs") or [])))
    time.sleep(0.3)
