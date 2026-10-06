# -*- coding: utf-8 -*-
"""第四轮：① 分类参数是否真的改变结果 ② 批量取歌一次能带多少 id"""
import json, urllib.request, urllib.parse, ssl, time

CTX = ssl.create_default_context(); CTX.check_hostname = False; CTX.verify_mode = ssl.CERT_NONE
HDR = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36",
       "Referer": "https://music.163.com/", "Accept": "application/json, text/plain, */*"}


def get(url, data=None, timeout=40):
    try:
        req = urllib.request.Request(url, data=data, headers=HDR if data is None else
                                     {**HDR, "Content-Type": "application/x-www-form-urlencoded"})
        with urllib.request.urlopen(req, timeout=timeout, context=CTX) as r:
            return json.loads(r.read().decode("utf-8", "replace"))
    except Exception as e:
        return {"__err__": "%s: %s" % (type(e).__name__, e)}


print("=== ① 分类是否生效 ===")
for cat in ["全部", "流行", "古典", "电子", "民谣"]:
    d = get("https://music.163.com/api/playlist/list?cat=%s&order=hot&limit=8&offset=0" % urllib.parse.quote(cat))
    arr = d.get("playlists") or []
    print("  %-4s total=%-6s 首条=%s" % (cat, d.get("total"), [p.get("name")[:14] for p in arr[:3]]))
    time.sleep(0.3)

print("\n=== ② 大 offset 是否还能出新歌单 ===")
for off in [0, 600, 1500, 3000, 5000]:
    d = get("https://music.163.com/api/playlist/list?cat=%s&order=hot&limit=8&offset=%d" % (urllib.parse.quote("流行"), off))
    arr = d.get("playlists") or []
    print("  offset=%-5d 回包=%-3d 首条=%s" % (off, len(arr), [p.get("name")[:14] for p in arr[:2]]))
    time.sleep(0.3)

print("\n=== ③ song/detail 单次上限 ===")
ids = list(range(186016, 186016 + 1000))
for n in [200, 500, 1000]:
    body = "c=" + urllib.parse.quote(json.dumps([{"id": i} for i in ids[:n]], separators=(",", ":")))
    t0 = time.time()
    d = get("https://music.163.com/api/v3/song/detail", data=body.encode())
    songs = (d or {}).get("songs") or []
    print("  请求 %-5d → 回包 %-5d  %.1fs  code=%s" % (n, len(songs), time.time() - t0, (d or {}).get("code")))
    time.sleep(0.4)

print("\n=== ④ trackIds 完整度抽查（大歌单） ===")
pl = json.load(open("data/catalog/_playlists.json", encoding="utf-8"))["playlists"]
for p in [x for x in pl if x["n"] >= 400][:2]:
    d = get("https://music.163.com/api/v6/playlist/detail?id=%d&n=1000&s=8" % p["id"])
    pp = (d or {}).get("playlist") or {}
    print("  %s  trackCount=%s trackIds=%d" % (p["name"][:18], pp.get("trackCount"), len(pp.get("trackIds") or [])))
    time.sleep(0.3)
