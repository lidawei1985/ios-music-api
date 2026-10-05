#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
probe.py —— 公网连通性 & 真取链 决定性验证（零第三方依赖，纯标准库）

要回答两个问题：
  A. GitHub Actions（公网 Ubuntu runner）能不能连通各音乐平台接口？
  B. 能不能真的拿到「可播放直链」？（不是搜索命中，是 Range GET 拿回音频字节）

产出：probe_result.json + 控制台表格
"""
import json
import socket
import time
import urllib.request
import urllib.parse
import urllib.error
from concurrent.futures import ThreadPoolExecutor

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36")
RESULT = {"dns": {}, "http": [], "chain": {}, "summary": {}}


def http(url, headers=None, data=None, method=None, timeout=15, read=0):
    """返回 (status, body_text, elapsed_ms, err)"""
    h = {"User-Agent": UA, "Accept": "*/*"}
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, data=data, headers=h, method=method)
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read(read) if read else r.read()
            ms = int((time.time() - t0) * 1000)
            try:
                return r.status, raw.decode("utf-8", "replace"), ms, None
            except Exception:
                return r.status, "", ms, None
    except urllib.error.HTTPError as e:
        ms = int((time.time() - t0) * 1000)
        try:
            return e.code, e.read(2000).decode("utf-8", "replace"), ms, None
        except Exception:
            return e.code, "", ms, None
    except Exception as e:
        return None, "", int((time.time() - t0) * 1000), f"{type(e).__name__}: {e}"


def dns(host):
    t0 = time.time()
    try:
        infos = socket.getaddrinfo(host, None)
        ips = sorted({i[4][0] for i in infos})
        return {"ok": True, "ips": ips[:4], "ms": int((time.time() - t0) * 1000)}
    except Exception as e:
        return {"ok": False, "err": f"{type(e).__name__}: {e}", "ms": int((time.time() - t0) * 1000)}


# ---------------------------------------------------------------- A. 连通性
ENDPOINTS = [
    ("qq-search",  "https://c.y.qq.com/soso/fcgi-bin/client_search_cp?w=%E7%A8%BB%E9%A6%99&format=json&n=3&p=1&new_json=1", None),
    ("qq-chart",   "https://c.y.qq.com/v8/fcg-bin/fcg_v8_toplist_cp.fcg?topid=26&format=json&page=detail&type=top&song_begin=0&song_num=10", None),
    ("qq-hotkey",  "https://c.y.qq.com/splcloud/fcgi-bin/gethotkey.fcg", None),
    ("qq-lyric",   "https://c.y.qq.com/lyric/fcgi-bin/fcg_query_lyric_new.fcg?songmid=003OUlho2HcRHC&format=json&nobase64=1", {"Referer": "https://y.qq.com/portal/player.html"}),
    ("qq-uapi",    "https://u.y.qq.com/cgi-bin/musicu.fcg", None),
    ("kg-search",  "http://mobilecdn.kugou.com/api/v3/search/song?format=json&keyword=%E7%A8%BB%E9%A6%99&page=1&pagesize=3&showtype=1", None),
    ("kg-singer",  "http://mobilecdn.kugou.com/api/v3/search/singer?format=json&keyword=%E5%91%A8%E6%9D%B0%E4%BC%A6&page=1&pagesize=3", None),
    ("wy-search",  "https://music.163.com/api/search/get?s=%E7%A8%BB%E9%A6%99&type=1&limit=3&offset=0", {"Referer": "https://music.163.com/"}),
    ("bilisearch", "https://api.bilibili.com/x/web-interface/search/type?search_type=video&keyword=%E7%A8%BB%E9%A6%99", None),
    ("migu",       "https://m.music.migu.cn/migu/remoting/scr_search_tag?keyword=%E7%A8%BB%E9%A6%99&type=2&rows=3&pgc=1", None),
    ("jsdelivr",   "https://cdn.jsdelivr.net/gh/lidawei1985/ios-tv-api@main/README.md", None),
    ("purge",      "https://purge.jsdelivr.net/gh/lidawei1985/ios-tv-api@main/README.md", None),
]

HOSTS = ["c.y.qq.com", "u.y.qq.com", "y.gtimg.cn", "dl.stream.qqmusic.qq.com",
         "isure.stream.qqmusic.qq.com", "ws.stream.qqmusic.qq.com",
         "mobilecdn.kugou.com", "m.kugou.com", "music.163.com", "api.bilibili.com"]


def run_connectivity():
    with ThreadPoolExecutor(max_workers=12) as ex:
        for h, r in zip(HOSTS, ex.map(dns, HOSTS)):
            RESULT["dns"][h] = r
    def one(item):
        name, url, hdr = item
        st, body, ms, err = http(url, headers=hdr, timeout=15)
        return {"name": name, "status": st, "ms": ms, "err": err,
                "bytes": len(body), "head": body[:110].replace("\n", " ")}
    with ThreadPoolExecutor(max_workers=12) as ex:
        RESULT["http"] = list(ex.map(one, ENDPOINTS))


# ---------------------------------------------------------------- B. 真取链
def range_probe(url, headers=None, label=""):
    """Range GET 前 2KB —— 唯一判据：拿到真音频字节"""
    h = dict(headers or {})
    h["Range"] = "bytes=0-2047"
    st, body, ms, err = http(url, headers=h, timeout=20, read=2048)
    return {"url": url[:160], "status": st, "ms": ms, "err": err, "bytes": len(body), "label": label}


def chain_qq(kw="稻香"):
    out = {"steps": []}
    st, body, ms, err = http("https://c.y.qq.com/soso/fcgi-bin/client_search_cp?w=" +
                             urllib.parse.quote(kw) + "&format=json&n=3&p=1&new_json=1", timeout=20)
    out["steps"].append({"step": "search", "status": st, "ms": ms, "err": err})
    if not body:
        return out
    try:
        d = json.loads(body)
        song = d["data"]["song"]["list"][0]
    except Exception as e:
        out["steps"].append({"step": "parse", "err": f"{type(e).__name__}: {e}", "head": body[:200]})
        return out
    mid = song.get("mid") or song.get("songmid")
    media_mid = (song.get("file") or {}).get("media_mid") or mid
    out["song"] = {"mid": mid, "media_mid": media_mid, "title": song.get("title"),
                   "singer": "/".join(s.get("name", "") for s in song.get("singer", []))}
    out["steps"].append({"step": "search-ok", "title": out["song"]["title"]})

    filename = f"M500{mid}{media_mid}.mp3"
    out["filename"] = filename
    variants = [
        ("music.vkey.GetVkeyServer", "UrlGetVkey"),
        ("vkey.GetVkeyServer", "CgiGetVkey"),
    ]
    for module, method in variants:
        payload = {"comm": {"uin": 0, "format": "json", "ct": 24, "cv": 0, "platform": "20"},
                   "req": {"module": module, "method": method,
                           "param": {"guid": "10000", "songmid": [mid], "songtype": [0],
                                     "uin": "0", "loginflag": 0, "platform": "20",
                                     "filename": [filename]}}}
        st, body, ms, err = http("https://u.y.qq.com/cgi-bin/musicu.fcg",
                                 headers={"Content-Type": "application/json",
                                          "Referer": "https://y.qq.com/"},
                                 data=json.dumps(payload).encode(), timeout=20)
        tag = f"{module}/{method}"
        if not body:
            out["steps"].append({"step": tag, "status": st, "ms": ms, "err": err})
            continue
        try:
            j = json.loads(body)
            data = (j.get("req") or {}).get("data") or {}
            sip = data.get("sip") or []
            infos = data.get("midurlinfo") or []
            purl = infos[0].get("purl") if infos else None
            out["steps"].append({"step": tag, "status": st, "ms": ms, "sip": sip[:1],
                                 "purl": (purl or "")[:80], "code": j.get("code"),
                                 "result": (infos[0] or {}).get("result") if infos else None})
            if purl:
                url = (sip[0] if sip else "http://dl.stream.qqmusic.qq.com/") + purl
                out["direct_url"] = url
                out["range"] = range_probe(url, {"Referer": "https://y.qq.com/"}, tag)
                if out["range"].get("status") in (200, 206):
                    break
        except Exception as e:
            out["steps"].append({"step": tag, "parse_err": f"{type(e).__name__}: {e}", "head": body[:200]})
    return out


def chain_kg(kw="稻香"):
    out = {"steps": []}
    st, body, ms, err = http("http://mobilecdn.kugou.com/api/v3/search/song?format=json&keyword=" +
                             urllib.parse.quote(kw) + "&page=1&pagesize=3&showtype=1", timeout=20)
    out["steps"].append({"step": "search", "status": st, "ms": ms, "err": err})
    if not body:
        return out
    try:
        info = json.loads(body)["data"]["info"][0]
    except Exception as e:
        out["steps"].append({"step": "parse", "err": f"{type(e).__name__}: {e}", "head": body[:200]})
        return out
    h = info.get("hash")
    out["song"] = {"hash": h, "title": info.get("songname"), "singer": info.get("singername"),
                   "album_id": info.get("album_id"), "album_img": (info.get("album_img") or "")[:90]}
    for cmd_url in (f"http://m.kugou.com/app/i/getSongInfo.php?cmd=playInfo&hash={h}",
                    f"https://www.kugou.com/yy/index.php?r=play/getdata&hash={h}&album_id={info.get('album_id')}"):
        st, body, ms, err = http(cmd_url, headers={"Referer": "https://www.kugou.com/"}, timeout=20)
        tag = "getSongInfo" if "getSongInfo" in cmd_url else "yy/getdata"
        if not body:
            out["steps"].append({"step": tag, "status": st, "ms": ms, "err": err})
            continue
        try:
            j = json.loads(body)
            u = j.get("url") or (j.get("data") or {}).get("play_url")
            out["steps"].append({"step": tag, "status": st, "ms": ms,
                                 "url": (u or "")[:110], "keys": list(j.keys())[:8]})
            if u:
                out["direct_url"] = u
                out["range"] = range_probe(u, {"Referer": "https://www.kugou.com/"}, tag)
                if out["range"].get("status") in (200, 206):
                    break
        except Exception as e:
            out["steps"].append({"step": tag, "parse_err": f"{type(e).__name__}: {e}", "head": body[:200]})
    return out


def chain_wy(kw="稻香"):
    out = {"steps": []}
    st, body, ms, err = http("https://music.163.com/api/search/get?s=" + urllib.parse.quote(kw) +
                             "&type=1&limit=3&offset=0", headers={"Referer": "https://music.163.com/"}, timeout=20)
    out["steps"].append({"step": "search", "status": st, "ms": ms, "err": err})
    if body:
        try:
            s = json.loads(body)["result"]["songs"][0]
            out["song"] = {"id": s["id"], "name": s["name"],
                           "singer": "/".join(a["name"] for a in s.get("artists", []))}
            st2, body2, ms2, err2 = http(f"https://music.163.com/api/song/enhance/player/url?id={s['id']}&ids=[{s['id']}]&br=320000",
                                         headers={"Referer": "https://music.163.com/"}, timeout=20)
            try:
                dd = json.loads(body2)["data"][0]
                out["steps"].append({"step": "player/url(匿名)", "status": st2, "ms": ms2,
                                     "url": (dd.get("url") or "None")[:90], "code": dd.get("code"),
                                     "reason": dd.get("freeTrialInfo")})
            except Exception as e:
                out["steps"].append({"step": "player/url(匿名)", "err": f"{type(e).__name__}: {e}", "head": (body2 or "")[:160]})
        except Exception as e:
            out["steps"].append({"step": "parse", "err": f"{type(e).__name__}: {e}", "head": body[:200]})
    return out


# ---------------------------------------------------------------- main
if __name__ == "__main__":
    print("=" * 78)
    print("A. 公网连通性")
    run_connectivity()
    print(f"{'host':<34} {'DNS':<6} {'IP':<18} {'ms'}")
    for h in HOSTS:
        r = RESULT["dns"][h]
        ip = (r.get("ips") or [""])[0] if r.get("ok") else "-"
        print(f"{h:<34} {'OK' if r['ok'] else 'FAIL':<6} {ip:<18} {r['ms']}")
    print()
    print(f"{'endpoint':<12} {'HTTP':<6} {'ms':<7} {'bytes':<8} head")
    for e in RESULT["http"]:
        print(f"{e['name']:<12} {str(e['status']):<6} {e['ms']:<7} {e['bytes']:<8} {e['head'][:70]}")
    print("\n" + "=" * 78)
    print("B. 真取链（Range GET 前 2KB 能否拿回音频）")
    for fn, label in ((chain_qq, "QQ"), (chain_kg, "酷狗"), (chain_wy, "网易云")):
        try:
            r = fn()
        except Exception as e:
            r = {"fatal": f"{type(e).__name__}: {e}"}
        RESULT["chain"][label] = r
        print(f"\n--- {label} ---")
        for s in r.get("steps", []):
            print("   ", json.dumps(s, ensure_ascii=False)[:220])
        if r.get("song"):
            print("    曲目:", json.dumps(r["song"], ensure_ascii=False)[:150])
        print("    直链:", (r.get("direct_url") or "(未拿到)")[:150])
        print("    取流:", json.dumps(r.get("range", {}), ensure_ascii=False)[:180])

    ok = [k for k, v in RESULT["chain"].items()
          if (v.get("range") or {}).get("status") in (200, 206)]
    RESULT["summary"] = {
        "dns_ok": sum(1 for v in RESULT["dns"].values() if v["ok"]),
        "dns_total": len(RESULT["dns"]),
        "http_ok": sum(1 for e in RESULT["http"] if e["status"] == 200),
        "http_total": len(RESULT["http"]),
        "direct_link_ok": ok,
    }
    print("\n" + "=" * 78)
    print("结论:", json.dumps(RESULT["summary"], ensure_ascii=False))
    with open("probe_result.json", "w", encoding="utf-8") as f:
        json.dump(RESULT, f, ensure_ascii=False, indent=1)
    print("已写 probe_result.json")
