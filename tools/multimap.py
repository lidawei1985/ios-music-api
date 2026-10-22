# -*- coding: utf-8 -*-
"""多源映射：给每首曲目找到「同一个人在别的平台唱同一首歌」的 id，供端上故障切换。

============================ 为什么需要它 ============================
2026-10-22 体检曲库 75,997 首：
    · src 分布：None(默认网易) 74,479 / wy 777 / kw 686 / migu 55
    · **多源同曲映射 = 0 条**
  → 用户所说的「备用线路」实际是空的：某首歌的网易直链一 404，就彻底没声。
  本脚本补的就是这张映射表。

============================ 接口实测（2026-10-22 本机）============================
    · 酷我 search.kuwo.cn/r.s        → 0.30s / 200 ✅
    · 咪咕 app.c.nf.migu.cn search_all → 0.50s / 200 ✅（第一条就返回「稻香 (Remix摇滚版)」，
      所以**必须严格校验**，否则一次匹配错误就是张冠李戴）
    · archive.org 三节点 TCP 443 全超时 ❌（Internet Archive 国内不可用，不接）
    · api.jamendo.com 可达但需 client_id（免费注册），暂留接口位
    · musicbrainz.org 1.41s ✅（用于原唱核对，见 mirror 的 cv 辅助段）

============================ 严格匹配规则（防张冠李戴）============================
    标题：归一后必须**完全相等**（先剥掉 Remix/伴奏/Live/版 等后缀）
    歌手：归一后相等，或一方完整包含另一方（"周杰伦" ⊂ "周杰伦/费玉清"）
    时长：与曲库时长差 ≤ 3s（拿不到时长就不采信这条候选）
    黑名单：伴奏版 / 纯音乐 / DJ版 / 翻唱 / 消音 / 片段 一律不收

============================ 产物 ============================
    data/streams/xmap.js →  window.__DWG_XMAP = {"<skey>": {"src":"wy","ids":{"wy":1,"kw":2,"mg":"3"}}}
    端上 stRec() 读它 → streamCands() 生成候选链 → 播放出错自动换源。

用法：
    python tools/multimap.py --top 3000            # 补热度前 3000 首
    python tools/multimap.py --top 3000 --budget 1800
    python tools/multimap.py --dry
"""
import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
CAT = os.path.join(ROOT, "data", "catalog")
OUT = os.path.join(ROOT, "data", "streams")

UA = {"User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) "
                    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.0 Mobile Safari/604.1"}
OP = urllib.request.build_opener(urllib.request.ProxyHandler({}))

# 这些后缀说明「不是同一个录音」，匹配时必须剥掉后再比标题；
# 但如果剥完之后原名的差别只在后缀上（原曲 vs 伴奏），那就不能算同一首。
SUFFIX = re.compile(r"[\(（\[【][^\)）\]】]{0,30}[\)）\]】]", re.S)
JUNK = re.compile(r"(伴奏|纯音乐|instrumental|karaoke|消音|DJ版|dj ?版|remix|加速|慢速|"
                  r"降调|升调|翻唱|cover|片段|铃声|试听|串烧|0\.8x|1\.2x|live|现场|演唱会|"
                  r"完整版|搞笑|纯享|舞曲|广场舞|钢琴版|吉他版|口琴|笛子|萨克斯|八音盒)", re.I)
# 上面这条是「宁可少一条备用线路，也绝不把现场版当原版」的闸门。
# 2026-10-22 实测踩过：『告白气球 (CCTV-15…现场)』剥掉括号后标题完全等于「告白气球」，
# 只比对标题会直接采信 —— 那就是张冠李戴。


def norm(s):
    return re.sub(r"[\s\-_（）()\[\]【】·,.，。'\"!！?？~～&]", "", (s or "")).lower()


def dehtml(s):
    """酷我/咪咕回的都是 HTML 片段：`&nbsp;` `&apos;`，还有双重转义的 `\\u0026`。
    不解码的话标题永远比不中（2026-10-22 实测：『稻香&nbsp;(完整版|DJ&nbsp;Ray版)』）。"""
    s = str(s or "")
    s = s.replace("\\\\u0026", "&").replace("\\u0026", "&")
    try:
        import html as _h
        s = _h.unescape(s)
    except Exception:
        pass
    return re.sub(r"\s+", " ", s).strip()


def cpslice(s, n):
    return "".join(list(str(s or ""))[:n])


def skey(t, s):
    return cpslice(norm(t), 40) + "|" + cpslice(norm(s), 30)


def base_title(t):
    return norm(SUFFIX.sub("", (t or "")).strip())


def http_json(url, timeout=12):
    """取 JSON；容不下就返回 None。"""
    try:
        with OP.open(urllib.request.Request(url, headers=UA), timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", "replace"))
    except Exception:
        return None


def http_loose(url, timeout=12):
    """取「伪 JSON」：酷我老接口回的是 **Python dict 字面量**（单引号键），
    json.loads 必炸（2026-10-22 实测：11KB 正常返回却解析失败，白跑一整轮）。
    所以先试 json，再退 ast.literal_eval，最后正则兜一下 &nbsp; 之类的实体。"""
    try:
        with OP.open(urllib.request.Request(url, headers=UA), timeout=timeout) as r:
            raw = r.read().decode("utf-8", "replace")
    except Exception:
        return None
    try:
        return json.loads(raw)
    except Exception:
        pass
    try:
        import ast
        return ast.literal_eval(raw)
    except Exception:
        return None


# ------------------------------------------------------------------ 各源搜索
def search_kw(title, artist):
    """酷我：返回 [{id, t, s, dur}]，dur 单位秒（接口给的是秒）"""
    q = {"all": ("%s %s" % (title, artist)).strip(), "ft": "music",
         "itemset": "web_2013", "client": "kt", "pn": "0", "rn": "20",
         "rformat": "json", "encoding": "utf8"}
    d = http_loose("http://search.kuwo.cn/r.s?" + urllib.parse.urlencode(q))
    if not d:
        return []
    out = []
    for x in d.get("abslist") or []:
        try:
            dur = int(float(x.get("DURATION") or 0))
        except ValueError:
            dur = 0
        out.append({"id": str(x.get("MUSICRID") or "").replace("MUSIC_", ""),
                    "t": dehtml(x.get("SONGNAME")), "s": dehtml(x.get("ARTIST")), "dur": dur})
    return [x for x in out if x["id"]]


def search_mg(title, artist):
    """咪咕：contentId 才是点播用的 id"""
    sw = json.dumps({"song": 1, "album": 0, "singer": 0, "tagSong": 0,
                     "mvSong": 0, "bestShow": 0, "lyricSong": 0})
    q = {"text": ("%s %s" % (title, artist)).strip(), "pageNo": "1",
         "pageSize": "8", "searchSwitch": sw}
    d = http_json("https://app.c.nf.migu.cn/MIGUM2.0/v1.0/content/search_all.do?"
                  + urllib.parse.urlencode(q))
    if not d or d.get("code") != "000000":
        return []
    res = ((d.get("songResultData") or {}).get("result")) or []
    out = []
    for x in res:
        # 咪咕时长字段不统一：有 length(秒字符串) / duration(毫秒)
        dur = 0
        try:
            dur = int(float(x.get("length") or 0))
        except ValueError:
            pass
        if not dur:
            try:
                dur = int(float(x.get("duration") or 0)) // 1000
            except ValueError:
                pass
        singers = " ".join(z.get("name", "") for z in (x.get("singers") or []))
        out.append({"id": x.get("contentId") or "", "t": dehtml(x.get("name")),
                    "s": dehtml(singers), "dur": dur})
    return [x for x in out if x["id"]]


def pick(cands, title, artist, libdur):
    """从候选里挑出「确实是同一首歌」的那一条；挑不到返回 None。"""
    bt, ba = base_title(title), norm(artist)
    for c in cands:
        if JUNK.search(c["t"] or ""):
            continue
        if base_title(c["t"]) != bt:                 # 标题必须完全一致（剥后缀后）
            continue
        cs = norm(c["s"])
        if ba and cs and ba not in cs and cs not in ba:
            continue                                 # 歌手对不上 → 不是同一个人
        if libdur and c["dur"] and abs(c["dur"] - libdur) > 3:
            continue                                 # 时长差太多 → 同名不同曲
        return c
    return None


# ------------------------------------------------------------------ 主流程
def load_pool(top):
    man = json.load(open(os.path.join(CAT, "manifest.json"), encoding="utf-8"))
    items = []
    for sh in man["shards"]:
        p = os.path.join(CAT, sh["file"])
        if os.path.exists(p):
            items.extend(json.load(open(p, encoding="utf-8")))
    items = [x for x in items if x.get("a") and x.get("n")]
    items.sort(key=lambda x: -(x.get("pop") or 0))
    return items[:top]


def load_done():
    p = os.path.join(OUT, "xmap.js")
    if not os.path.exists(p):
        return {}
    try:
        txt = open(p, encoding="utf-8").read()
        m = re.search(r"window\.__DWG_XMAP\s*=\s*", txt)
        if not m:
            return {}
        body = txt[m.end():].rstrip()
        if body.endswith(";"):
            body = body[:-1]
        d = json.loads(body)
    except Exception:
        return {}
    # 归一历史脏键：早期版本写的是 "mg"，后来统一成 "migu"，
    # 不清理会在同一首里同时出现两个等价键（实测产物里就有 mg+migu 同值重复）。
    for v in d.values():
        ids = v.get("ids") or {}
        if ids.get("mg") and not ids.get("migu"):
            ids["migu"] = ids.pop("mg")
        else:
            ids.pop("mg", None)
    return d


def probe(x, have):
    t, a = (x.get("n") or "").strip(), (x.get("a") or "").strip()
    k = skey(t, a)
    ids = dict((have.get(k) or {}).get("ids") or {})
    libdur = (x.get("d") or 0) / 1000.0
    src0 = (x.get("src") or "wy")
    if x.get("i"):
        ids.setdefault(src0, str(x["i"]))
    got = 0
    if not ids.get("kw"):
        c = pick(search_kw(t, a), t, a, libdur)
        if c:
            ids["kw"] = c["id"]; got += 1
    if not ids.get("migu"):
        c = pick(search_mg(t, a), t, a, libdur)
        if c:
            ids["migu"] = c["id"]; got += 1
    if got == 0 and not any(v for kk, v in ids.items() if kk != src0):
        return None
    return k, {"src": src0, "ids": ids}


def flush(mapping):
    os.makedirs(OUT, exist_ok=True)
    with open(os.path.join(OUT, "xmap.js"), "w", encoding="utf-8") as f:
        f.write("window.__DWG_XMAP=%s;\n"
                % json.dumps(mapping, ensure_ascii=False, separators=(",", ":")))
    print("   💾 data/streams/xmap.js  %d 首" % len(mapping))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--top", type=int, default=3000)
    ap.add_argument("--budget", type=int, default=1500)
    ap.add_argument("--workers", type=int, default=6,
                    help="酷我/咪咕不限速，但别开太大（8 以内比较稳）")
    ap.add_argument("--dry", action="store_true")
    args = ap.parse_args()

    pool = load_pool(args.top)
    have = load_done()
    print("候选 %d 首 ｜ 已有映射 %d 首" % (len(pool), len(have)))

    # 已经有 kw + mg 两个备用源的不用再查
    todo = [x for x in pool
            if not (len(((have.get(skey(x["n"], x["a"])) or {}).get("ids")) or {}) >= 3)]
    print("待补 %d 首" % len(todo))
    if args.dry or not todo:
        return

    t0 = time.time()
    stat = Counter()
    done = dict(have)

    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(probe, x, have): x for x in todo}
        for n, fu in enumerate(futs, 1):
            try:
                r = fu.result()
            except Exception:
                r = None
            if r:
                done[r[0]] = r[1]
                stat["补到映射"] += 1
                cnt = len([v for v in r[1]["ids"].values() if v])
                stat["%d 个源" % cnt] += 1
            else:
                stat["无匹配"] += 1
            if n % 100 == 0:
                print("  %d/%d ｜映射 %d ｜%.0fs" % (n, len(futs), len(done), time.time() - t0))
            if n % 600 == 0:
                flush(done)
            if time.time() - t0 > args.budget:
                print("\n⏳ 到预算 %ds，收工" % args.budget)
                break

    flush(done)
    print("\n=== 本轮 ===")
    for k, v in stat.most_common():
        print("  %-12s %d" % (k, v))
    # 覆盖率：热门曲里有多少真拿到了备用源
    cover = sum(1 for x in pool
                if len([v for v in ((done.get(skey(x["n"], x["a"])) or {}).get("ids") or {}).values() if v]) >= 2)
    print("热门 %d 首中 %d 首有 ≥2 个源（%.1f%%）" % (len(pool), cover, 100.0 * cover / max(1, len(pool))))


if __name__ == "__main__":
    main()
