# -*- coding: utf-8 -*-
"""第二轮：验证「歌单 trackIds → 批量 song/detail」这条主干的单次产出与字段完整度。"""
import json, urllib.request, urllib.parse, ssl, time

CTX = ssl.create_default_context(); CTX.check_hostname = False; CTX.verify_mode = ssl.CERT_NONE
HDR = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36",
       "Referer": "https://music.163.com/", "Accept": "application/json, text/plain, */*"}


def get(url, hdr=None, timeout=25):
    req = urllib.request.Request(url, headers=hdr or HDR)
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=CTX) as r:
            return r.status, r.read()
    except Exception as e:
        return None, ("%s: %s" % (type(e).__name__, e)).encode()


def j(b):
    try: return json.loads(b.decode("utf-8", "replace"))
    except Exception: return None


def song_detail(ids):
    """批量取歌：1000 个一批"""
    body = json.dumps([{"id": int(i)} for i in ids], separators=(",", ":"))
    if len(ids) == 1:
        body = "[" + body[1:-1] + "]"
    data = ("c=" + urllib.parse.quote(body)).encode()
    req = urllib.request.Request("https://music.163.com/api/v3/song/detail", data=data,
                                 headers={**HDR, "Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req, timeout=40, context=CTX) as r:
            return j(r.read())
    except Exception as e:
        print("   song/detail err", type(e).__name__, e)
        return None


# 1) 热歌榜 trackIds
st, b = get("https://music.163.com/api/v6/playlist/detail?id=3778678&n=1000&s=8")
d = j(b) or {}
p = d.get("playlist") or {}
ids = [t["id"] for t in (p.get("trackIds") or [])] if p.get("trackIds") and isinstance(p["trackIds"][0], dict) else (p.get("trackIds") or [])
print("热歌榜 trackIds =", len(ids), "| v6 tracks 回包 =", len(p.get("tracks") or []),
      "| trackCount =", p.get("trackCount"))
if p.get("tracks"):
    s0 = p["tracks"][0]
    print("  sample fields:", sorted(s0.keys())[:40])
    print("  name/artist/album/cover/dur:",
          s0.get("name"), "|", (s0.get("ar") or [{}])[0].get("name"),
          "|", (s0.get("al") or {}).get("name"), "|", (s0.get("al") or {}).get("picUrl"), "|", s0.get("dt"))

# 2) 批量 song/detail（1000）
t0 = time.time()
d2 = song_detail(ids[:1000])
songs = (d2 or {}).get("songs") or []
print("song/detail 回包 songs =", len(songs), "  %.1fs" % (time.time() - t0))
if songs:
    s = songs[0]
    print("  fields:", sorted(s.keys())[:40])
    # 判定可播性字段
    fee = s.get("fee")
    print("  name=", s.get("name"), "| fee=", fee, "| dt=", s.get("dt"),
          "| al=", (s.get("al") or {}).get("name"), "| pic=", ((s.get("al") or {}).get("picUrl") or "")[:60],
          "| ar=", "/".join(a.get("name", "") for a in (s.get("ar") or [])))
    nopic = sum(1 for x in songs if not ((x.get("al") or {}).get("picUrl")))
    noplen = sum(1 for x in songs if not x.get("dt"))
    print("  无封面 %d / 无时长 %d" % (nopic, noplen))

# 3) 歌单枚举上限：多分类翻页
import collections
tot = collections.Counter()
for cat in ["全部", "流行", "摇滚", "电子"]:
    for off in [0, 60, 120, 600, 1200]:
        st, b = get("https://music.163.com/api/playlist/list?cat=%s&order=hot&limit=60&offset=%d" % (urllib.parse.quote(cat), off))
        dd = j(b) or {}
        pls = dd.get("playlists") or []
        print("  cat=%-4s offset=%-5d total=%-6s 回包=%d" % (cat, off, dd.get("total"), len(pls)))
        tot[cat] = max(tot[cat], len(pls))
