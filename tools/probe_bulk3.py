# -*- coding: utf-8 -*-
"""第三轮：找真正能一次拿满歌单全部曲目的接口。"""
import json, urllib.request, ssl, time

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


pl = json.load(open("data/catalog/_playlists.json", encoding="utf-8"))["playlists"]
big = [p for p in pl if p["n"] >= 300][:3]
print("候选大歌单:", [(p["id"], p["n"], p["name"][:16]) for p in big])

for _p in big:
    pid, tc, name = _p["id"], _p["n"], _p["name"]
    print("\n===== 歌单 %s (%s首) %s" % (pid, tc, name[:20]))
    d = get("https://music.163.com/api/v6/playlist/detail?id=%d&n=1000&s=8" % pid)
    p = (d or {}).get("playlist") or {}
    tids = p.get("trackIds") or []
    print("  v6: tracks=%d trackIds=%d trackCount=%s err=%s" % (len(p.get("tracks") or []), len(tids), p.get("trackCount"), (d or {}).get("__err__")))

    d2 = get("https://music.163.com/api/playlist/track/all?id=%d&limit=1000&offset=0" % pid)
    s2 = (d2 or {}).get("songs") or []
    print("  track/all: songs=%d code=%s err=%s" % (len(s2), (d2 or {}).get("code"), (d2 or {}).get("__err__")))
    if s2:
        x = s2[0]
        print("     sample:", x.get("name"), "|", (x.get("al") or {}).get("name"), "|",
              ((x.get("al") or {}).get("picUrl") or "")[:50], "| dt=", x.get("dt"), "| keys_ar=", bool(x.get("ar")))
    time.sleep(0.4)
