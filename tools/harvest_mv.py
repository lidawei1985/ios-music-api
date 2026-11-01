#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MV / 演唱会采集器 —— 治本：让 MV 页里放的是**用户真在听的那些歌**的 MV。

背景（2026-11-02 实测，这是本工具存在的全部理由）：
  旧 mv.json 有 4135 条，看着不少，但一比对就露馅：
    · 标题能对上端上可见曲目（用户真会听到的 5083 首）的：**72 条 = 1.7%**
    · 歌手能对上的：328 条 = 7.9%
    · 「热门 MV」1273 条里中文只占 5% —— 满屏韩团/日文/欧美 MV
  也就是说：用户听周深《大鱼》，翻 MV 页根本找不到《大鱼》。
  根因：MV 是拿网易「MV 全量库翻页」灌的，**跟曲库没有任何对齐关系**。

做法：**按曲库反查 MV**。对每首可见曲目去搜 `cloudsearch/pc?type=1004`：
  ① 歌名要对得上（归一化后相等或互相包含）
  ② 歌手要沾边 —— 否则「大鱼」会命中双笙/卢庚戌的翻唱 MV
  ③ 一次搜索两类都要：
       · 标题干净      → 归「曲库 MV」
       · 标题带 live/演唱会/现场/音乐会 → 归「演唱会现场」
  最后**真测**：mv/url 取直链 + Range 探 magic bytes，放不出来的不入库。

用法：
  python tools/harvest_mv.py --dry            # 只报数
  python tools/harvest_mv.py --limit 200
  python tools/harvest_mv.py                  # 全量
"""
import json, os, re, sys, time, urllib.request, urllib.parse, argparse
import concurrent.futures as cf

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import enrich_streams as E

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MV_FILE = os.path.join(ROOT, "data", "mv.json")

UA = {
    "User-Agent": ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 "
                   "(KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"),
    "Referer": "https://music.163.com/",
    "Cookie": "appver=2.9.7; os=pc",
}
OP = urllib.request.build_opener(urllib.request.ProxyHandler({}))
SEARCH = "https://music.163.com/api/cloudsearch/pc?s=%s&type=1004&offset=0&limit=12"
MVURL = "https://music.163.com/api/song/enhance/play/mv/url?id=%s&r=1080"

# live / 演唱会信号
LIVE_RE = re.compile(r"(live|现场|演唱会|音乐会|音乐节|歌友会|巡演|concert)", re.I)
# 翻唱 / 串歌信号（命中即毙：用户要的是本尊的 MV）
COVER_RE = re.compile(r"(cover|翻唱|翻自|cover\s|日语|韩语|英语版|女声mv|男声mv|舞蹈版|mmd|自作|自制)", re.I)


def get_json(u, timeout=15, tries=2):
    for _ in range(tries):
        try:
            r = OP.open(urllib.request.Request(u, headers=UA), timeout=timeout)
            return json.loads(r.read().decode("utf-8", "replace"))
        except Exception:
            time.sleep(0.5)
    return None


def magic_ok(h):
    return (h[4:8] == b"ftyp" or h[:3] == b"ID3"
            or (len(h) > 1 and h[0] == 0xFF and (h[1] & 0xE0) == 0xE0)
            or h[:4] == b"RIFF")


def probe(mid):
    """真测：mv/url 拿直链 + Range 探视频头。返回 (ok, why)"""
    d = get_json(MVURL % mid, timeout=15)
    if not d:
        return False, "api-err"
    url = ((d.get("data") or {}).get("url") or "")
    if not url.startswith("http"):
        return False, "no-url:%s" % d.get("code")
    try:
        r = OP.open(urllib.request.Request(url.replace("http:", "https:"),
                                           headers=dict(UA, Range="bytes=0-4095")), timeout=20)
        ct = (r.headers.get("Content-Type") or "").lower()
        if ct.startswith("text/"):
            return False, "html"
        if magic_ok(r.read(4096)):
            return True, "ok"
        return False, "ctype:" + ct[:14]
    except Exception as e:
        return False, "probe:" + repr(e)[:24]


def flat(x):
    """归一化：与端上 norm() 对齐思路 —— 抹掉空格再比，避免「A /B」vs「A/B」误判"""
    return E.norm(E.strip(x or "")).replace(" ", "")


def singer_ok(mv_artists, want):
    """歌手要沾边：任一歌手互相包含即算过"""
    if not want:
        return True
    w = flat(want)
    if not w:
        return True
    for a in mv_artists:
        fa = flat(a)
        if fa and (fa == w or fa in w or w in fa):
            return True
    return False


def pick(t, s, arr):
    """从搜索结果里挑：返回 [(id, name, artists, dur, play, is_live)]（最多 2 条：正片 + live）"""
    bt = flat(t)
    out, live = [], []
    for x in arr:
        nm = x.get("name") or ""
        artists = [a.get("name") or "" for a in (x.get("artists") or [])]
        an = "/".join(artists)
        if COVER_RE.search(nm):
            continue
        ct = flat(nm)
        # ① 歌名对得上（live 版本标题会带括号后缀，走包含判定）
        if ct == bt:
            sc = 100
        elif bt and (bt in ct or ct in bt):
            sc = 70
        else:
            continue
        # ② 歌手沾边
        if not singer_ok(artists, s):
            continue
        rec = (str(x.get("id") or ""), nm, an,
               int((x.get("duration") or 0) / 1000), int(x.get("playCount") or 0),
               x.get("cover") or x.get("imgurl") or "")
        if LIVE_RE.search(nm):
            live.append((sc, rec))
        else:
            out.append((sc, rec))
    # 播放量高的优先（同一首歌多个版本时，用户想看的是官方播放量高的那个）
    out.sort(key=lambda z: (-z[0], -z[1][4]))
    live.sort(key=lambda z: (-z[0], -z[1][4]))
    res = []
    if out:
        res.append((out[0][1], False))
    if live:
        res.append((live[0][1], True))
    return res


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--workers", type=int, default=8)
    a = ap.parse_args()

    cur = E.curated()
    mv = json.load(open(MV_FILE, encoding="utf-8"))
    have = set()
    for c in mv.get("collections") or []:
        for it in c.get("items") or []:
            have.add(str(it.get("id")))
    todo = sorted(cur.items(), key=lambda kv: -(kv[1].get("seen") or 0))
    if a.limit:
        todo = todo[:a.limit]
    print("待补 MV %d 首（库里已有 %d 条 MV）｜%d 并发" % (len(todo), len(have), a.workers), flush=True)

    stat = {}
    got_mv, got_live = [], []
    t0 = time.time()

    def work(kv):
        k, v = kv
        t, s = v.get("t") or "", v.get("s") or ""
        if not t:
            return k, [], "no-title"
        d = get_json(SEARCH % urllib.parse.quote((t + " " + s).strip()))
        arr = ((d or {}).get("result") or {}).get("mvs") or []
        if not arr:
            return k, [], "no-result"
        cands = pick(t, s, arr)
        if not cands:
            return k, [], "no-match"
        out = []
        for rec, is_live in cands:
            if rec[0] in have:
                out.append((rec, is_live, "have"))
                continue
            ok, why = probe(rec[0])
            out.append((rec, is_live, "ok" if ok else "dead:" + why))
        return k, out, "searched"

    with cf.ThreadPoolExecutor(a.workers) as ex:
        for n, (k, out, why) in enumerate(ex.map(work, todo), 1):
            stat[why.split(":")[0]] = stat.get(why.split(":")[0], 0) + 1
            for rec, is_live, st in out:
                key = st.split(":")[0]
                stat[key] = stat.get(key, 0) + 1
                if st == "ok":
                    item = {"id": rec[0], "t": rec[1], "a": rec[2], "cov": rec[5],
                            "dur": rec[3], "play": rec[4], "q": "1080P", "wh": ""}
                    (got_live if is_live else got_mv).append(item)
            if n % 50 == 0:
                print("  … %d/%d（MV %d / 演唱会 %d，%.0fs）"
                      % (n, len(todo), len(got_mv), len(got_live), time.time() - t0), flush=True)

    print("\n=== MV / 演唱会采集 ===")
    print("  正片 MV %d 条｜演唱会/现场 %d 条" % (len(got_mv), len(got_live)))
    for kk, vv in sorted(stat.items(), key=lambda x: -x[1]):
        print("    %-10s %d" % (kk, vv))
    if a.dry:
        print("  （--dry：未写库）样例：")
        for it in got_mv[:6]:
            print("    [MV] %s | %s (%ds)" % (it["t"], it["a"], it["dur"]))
        for it in got_live[:4]:
            print("    [Live] %s | %s (%ds)" % (it["t"], it["a"], it["dur"]))
        return

    cols = mv.setdefault("collections", [])
    idx = {c.get("name"): c for c in cols}
    for name, arr in (("曲库 MV", got_mv), ("演唱会现场", got_live)):
        c = idx.get(name)
        if not c:
            c = {"name": name, "count": 0, "items": []}
            cols.append(c)
            idx[name] = c
        have_ids = {str(x.get("id")) for x in c["items"]}
        for it in arr:
            if str(it["id"]) not in have_ids:
                c["items"].append(it)
                have_ids.add(str(it["id"]))
        c["items"].sort(key=lambda x: -(x.get("play") or 0))
        c["count"] = len(c["items"])
    mv["count"] = sum(len(c.get("items") or []) for c in cols)
    mv["updated"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    tmp = MV_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(mv, fh, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, MV_FILE)
    print("  已写入 data/mv.json（MV 总数 %d）" % mv["count"])


if __name__ == "__main__":
    main()
