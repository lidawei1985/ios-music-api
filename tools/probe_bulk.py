# -*- coding: utf-8 -*-
"""批量曲库来源摸底：找出能稳定翻页、单次产出最大的元数据接口。只读探测，不写库。"""
import json, urllib.request, urllib.parse, ssl, sys

CTX = ssl.create_default_context()
CTX.check_hostname = False
CTX.verify_mode = ssl.CERT_NONE

HDR = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36",
    "Referer": "https://music.163.com/",
    "Accept": "application/json, text/plain, */*",
}


def get(url, hdr=None, timeout=20):
    req = urllib.request.Request(url, headers=hdr or HDR)
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=CTX) as r:
            return r.status, r.read()
    except Exception as e:
        return None, ("%s: %s" % (type(e).__name__, e)).encode()


def j(b):
    try:
        return json.loads(b.decode("utf-8", "replace"))
    except Exception:
        return None


def t(name, url, hdr=None, probe=None):
    st, b = get(url, hdr)
    print("\n---", name, "->", st, "len=%d" % len(b))
    d = j(b)
    if d is None:
        print("   ", b[:200])
        return None
    if probe:
        try:
            probe(d)
        except Exception as e:
            print("    probe err", e)
    else:
        s = json.dumps(d, ensure_ascii=False)[:400]
        print("   ", s)
    return d


print("=" * 70)
print("A. 网易云 歌单目录 / 歌单列表")
t("playlist/catalogue", "https://music.163.com/api/playlist/catalogue", probe=lambda d: print("    cats:", len(d.get("categories", {})), "sub:", len(d.get("sub", []))))

t("playlist/list hot 全部", "https://music.163.com/api/playlist/list?cat=%s&order=hot&limit=60&offset=0" % urllib.parse.quote("全部"),
  probe=lambda d: print("    total:", d.get("total"), "playlists:", len(d.get("playlists") or []),
                        "first:", ((d.get("playlists") or [{}])[0].get("name"), (d.get("playlists") or [{}])[0].get("trackCount"))))

print("\n" + "=" * 70)
print("B. 网易云 歌单详情（单次曲目上限）")
# 用上面拿到的第一个歌单 id 试；没有就用手工热歌单
st, b = get("https://music.163.com/api/playlist/list?cat=%s&order=hot&limit=5&offset=0" % urllib.parse.quote("全部"))
d = j(b) or {}
pls = d.get("playlists") or []
pid = pls[0]["id"] if pls else 3778678
print("  测试歌单 id =", pid, (pls[0].get("name") if pls else ""))
t("playlist/detail n=1000", "https://music.163.com/api/v6/playlist/detail?id=%d&n=1000&s=8" % pid,
  probe=lambda x: print("    tracks:", len((x.get("playlist") or {}).get("tracks") or []),
                        "trackIds:", len((x.get("playlist") or {}).get("trackIds") or []),
                        "trackCount:", (x.get("playlist") or {}).get("trackCount")))
t("playlist/detail old n=1000", "https://music.163.com/api/playlist/detail?id=%d&n=1000&s=0" % pid,
  probe=lambda x: print("    result.tracks:", len((x.get("result") or {}).get("tracks") or []),
                        "trackIds:", len((x.get("result") or {}).get("trackIds") or []),
                        "trackCount:", (x.get("result") or {}).get("trackCount")))

print("\n" + "=" * 70)
print("C. 网易云 排行榜 / 歌手")
t("toplist", "https://music.163.com/api/toplist", probe=lambda x: print("    list:", len(x.get("list") or [])))
t("toplist/detail", "https://music.163.com/api/toplist/detail", probe=lambda x: print("    list:", len(x.get("list") or [])))
t("artist/top/song id=6452", "https://music.163.com/api/artist/top/song?id=6452",
  probe=lambda x: print("    songs:", len(x.get("songs") or [])))

print("\n" + "=" * 70)
print("D. QQ 音乐 分类歌单（现有 build_categories 用的）")
t("qq get_disslist", "https://c.y.qq.com/splcloud/fcgi-bin/fcg_get_diss_by_tag.fcg?picmid=1&rnd=0.1&g_tk=5381&loginUin=0&hostUin=0&format=json&inCharset=utf8&outCharset=utf-8&notice=0&platform=yqq.json&needNewCode=0&categoryId=10000000&sortId=5&sin=0&ein=59",
  hdr={"Referer": "https://y.qq.com/", "User-Agent": HDR["User-Agent"]},
  probe=lambda x: print("    total:", (x.get("data") or {}).get("total"), "list:", len((x.get("data") or {}).get("list") or [])))
