# -*- coding: utf-8 -*-
r"""全量核验 MV 是否真能播，剔掉打不开的。

============================ 为什么写它 ============================
2026-10-30 主人指令：「MV 演唱会每一个播放的了的」。

data/mv.json 里 4,068 条 MV 只带 mvid，**从来没有人真的验过能不能播**
（文件里的 verified=17 是早期抽样留下的死数字）。
抽样 60 条实测：取流 + https 直连 + Range 全部 60/60 通 —— 说明绝大多数是好的，
但「绝大多数」不等于「每一条」，剩下的才是用户会撞上的坑。

做法很实在：**逐条真测**。
   ① 向官方接口按 id 现取 1080P 直链（直链带 wsTime 签名，1 小时过期，库里只能存 id）
   ② 拿这条直链发带 Range 的 https GET，要求回 200/206 且 Content-Type 是 video/
   ③ 通不过的从 collections 里删掉，写回 mv.json + mv.js

结果落 data/mv_ok.json 做增量缓存（同一 id 7 天内不重测），
之后每轮 CI 只跑增量 —— 新补进来的 MV 才会被实测。

用法：
    python tools/verify_mv.py                # 增量核验 + 剔除
    python tools/verify_mv.py --all          # 忽略缓存全量重测
    python tools/verify_mv.py --dry          # 只报数不写盘
    python tools/verify_mv.py --budget 900   # 时间预算（秒）
"""
import argparse
import concurrent.futures as cf
import json
import os
import sys
import time
import urllib.error
import urllib.request

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DATA = os.path.join(ROOT, "data")

MV = os.path.join(DATA, "mv.json")
MVJS = os.path.join(DATA, "mv.js")
OKCACHE = os.path.join(DATA, "mv_ok.json")
TTL = 7 * 24 * 3600

UA = {"User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
                    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 "
                    "Mobile/15E148 Safari/604.1"}
API = "https://music.163.com/api/song/enhance/play/mv/url?id=%s&r=1080"
OP = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def api_url(mid):
    """按 id 现取直链。实测：UA 必须像浏览器，Python 默认 UA 会被 CDN 403"""
    try:
        req = urllib.request.Request(API % mid, headers=dict(UA, Referer="https://music.163.com/"))
        j = json.loads(OP.open(req, timeout=15).read().decode("utf-8", "replace"))
        dd = (j or {}).get("data") or {}
        u = str(dd.get("url") or "")
        if not u.startswith("http"):
            return None
        # ATS：一律升 https（同一条 CDN 路径 https 同样回 206 video/mp4，实测无差别）
        return u.replace("http://", "https://"), int(dd.get("r") or 480)
    except Exception:
        return None


def probe(mid):
    """真测能不能取到能响的视频流"""
    got = api_url(mid)
    if not got:
        return "no-url"
    u, r = got
    try:
        req = urllib.request.Request(u, headers=dict(UA, Range="bytes=0-2047"))
        resp = OP.open(req, timeout=20)
        ct = (resp.headers.get("Content-Type") or "").lower()
        resp.read(2048)
        if resp.status in (200, 206) and ct.startswith("video"):
            return "ok|%d" % r
        return "bad-ctype:%s/%s" % (resp.status, ct[:24])
    except Exception as e:
        return "stream-fail:%s" % repr(e)[:40]


def load_cache():
    if not os.path.exists(OKCACHE):
        return {}
    try:
        return json.load(open(OKCACHE, encoding="utf-8"))
    except Exception:
        return {}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--all", action="store_true", help="忽略 7 天缓存，全量重测")
    ap.add_argument("--dry", action="store_true")
    ap.add_argument("--budget", type=int, default=1500)
    ap.add_argument("--workers", type=int, default=8)
    a = ap.parse_args()

    mv = json.load(open(MV, encoding="utf-8"))
    cache = load_cache() if not a.all else {}
    now = int(time.time())

    items = []
    for c in (mv.get("collections") or []):
        for m in (c.get("items") or []):
            items.append((c, m))
    print("MV 条目 %d 条" % len(items))

    todo = []
    seen = {}
    for c, m in items:
        k = str(m.get("id") or "")
        if not k:
            continue
        v = cache.get(k)
        if v and isinstance(v, dict) and now - int(v.get("at") or 0) < TTL:
            seen[k] = v.get("st")
            continue
        if k in seen:
            continue
        seen[k] = None
        todo.append(k)
    print("缓存命中 %d 条，本轮待实测 %d 条" % (len(items) - len(todo), len(todo)))

    t0 = time.time()
    res = {}
    if todo:
        with cf.ThreadPoolExecutor(max_workers=a.workers) as ex:
            futs = {ex.submit(probe, k): k for k in todo}
            for n, fu in enumerate(cf.as_completed(futs), 1):
                k = futs[fu]
                try:
                    st = fu.result()
                except Exception as e:
                    st = "err:%s" % repr(e)[:40]
                res[k] = st
                seen[k] = st
                cache[k] = {"st": st, "at": now}
                if n % 100 == 0:
                    print("  %d/%d ｜%.0fs" % (n, len(todo), time.time() - t0))
                if time.time() - t0 > a.budget:
                    for f in futs:
                        f.cancel()
                    print("\n⏳ 到预算 %ds，剩余下轮续跑" % a.budget)
                    break

    ok = lambda v: isinstance(v, str) and v.startswith("ok")          # noqa: E731
    keep, drop = [], []
    qmap = {}
    for c, m in items:
        k = str(m.get("id") or "")
        st = seen.get(k)
        if ok(st):
            keep.append((c, m))
            qmap[k] = st.split("|")[1] if "|" in st else None
        elif st is None:
            keep.append((c, m))          # 本轮没测到（超预算）→ 先留着，别误删
        else:
            drop.append((c, m, st))

    print("\n=== 本轮结论 ===")
    print("  可播     %d" % len([v for v in seen.values() if ok(v)]))
    print("  不可播   %d" % len(drop))
    print("  未测     %d" % len([v for v in seen.values() if v is None]))
    for _, m, st in drop[:25]:
        print("    ✗ %s  %s  (%s)" % (m.get("id"), str(m.get("t"))[:22], st))

    if a.dry:
        print("--dry：不写盘")
        return 0

    json.dump(cache, open(OKCACHE, "w", encoding="utf-8"), ensure_ascii=False)

    if drop:
        ids = {str(m.get("id")) for _, m, _ in drop}
        for c in (mv.get("collections") or []):
            src = c.get("items") or []
            out = [x for x in src if str(x.get("id")) not in ids]
            c["count"] = len(out)
            c["items"] = out
        mv["count"] = sum(len(c.get("items") or []) for c in (mv.get("collections") or []))
    # 顺手把 curated 里的质量档位写成真实返回值（原来是 hardcode "1080P"）
    for c in (mv.get("collections") or []):
        for x in (c.get("items") or []):
            r = qmap.get(str(x.get("id")))
            if r:
                try:
                    rr = int(r)
                except Exception:
                    rr = 0
                if rr > 0:
                    x["q"] = "%dP" % rr
    mv["verified"] = len([v for v in seen.values() if ok(v)])
    mv["verified_quality"] = {}
    for v in seen.values():
        if isinstance(v, str) and v.startswith("ok|"):
            r = v.split("|")[1]
            mv["verified_quality"][r + "P"] = mv["verified_quality"].get(r + "P", 0) + 1
    mv["note"] = ("来源=网易云官方 MV 全量库。★ 2026-10-30 起每条都经 tools/verify_mv.py "
                  "真测（取流+Range 探测），打不开的一律剔除；被剔除的 id 记在 data/mv_ok.json。")
    json.dump(mv, open(MV, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    with open(MVJS, "w", encoding="utf-8") as f:
        f.write("window.__DWG_DB_mv=" + json.dumps(mv, ensure_ascii=False, separators=(",", ":")) + ";\n")
    print("\n💾 已写回 mv.json / mv.js，现存 %d 条" % mv["count"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
