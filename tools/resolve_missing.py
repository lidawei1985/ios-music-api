# -*- coding: utf-8 -*-
r"""★ 服务端「缺源曲目」翻译 + 真测落库 —— 根治「没资源直接跳过」。

============================= 背景（本机实测）=============================
2026-10-30 体检：端上可见曲目 5105 首
    · 带真实音源 1630 首（31.8%）
    · enrich_streams.py 用本机 7.6 万曲库索引补过一轮后，**仍有 3482 首译不出来**
      —— 它们的歌名/歌手在本机曲库里对不上（榜单用的是别名、合作写法不同、新歌未入库）。
剩下的这 3482 首，端上只能走 XFIND 在线搜（每次点播现搜，慢且依赖现场网络），
一旦搜不到就是用户看到的那句「暂无可用音源，跳过」。

本脚本把这活搬到服务端做掉：
    ① 网易云官方搜索接口，按「歌名 + 歌手」找候选
    ② 三关校验（与端上 XFIND 同一把尺子）：
        歌名归一化一致 / 互相包含；歌手必须沾边；时长误差 ≤8s；踢掉伴奏翻唱 DJ 现场
    ③ 拿到的 id **真打一次直链**，要求回 200/206 且 Content-Type 是 audio/
    ④ 通过才写进 data/streams.json；写不进的不撒谎，留给下轮
    ⑤ data/resolve_miss.json 记录「明确查不到」的曲目（连续 4 轮不再重试）

这样端上就是**开箱即用**：点开就有声，不用等在线搜。

用法：
    python tools/resolve_missing.py --dry            # 只报数
    python tools/resolve_missing.py --workers 8      # 实跑（默认预算 1500s）
    python tools/resolve_missing.py --budget 3000
"""
import argparse
import concurrent.futures as cf
import json
import os
import random
import re
import sys
import time
import urllib.parse
import urllib.request

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import enrich_streams as E                       # noqa: E402  （复用 skey / curated / strip / norm）

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DATA = os.path.join(ROOT, "data")
STREAM = os.path.join(DATA, "streams.json")
MISSF = os.path.join(DATA, "resolve_miss.json")

UA = {"User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
                    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 "
                    "Mobile/15E148 Safari/604.1"}
OP = urllib.request.build_opener(urllib.request.ProxyHandler({}))
SEARCH = "https://music.163.com/api/search/get/web?s=%s&type=1&offset=0&limit=12"
OUTER = "https://music.163.com/song/media/outer/url?id=%s.mp3"

# 与端上 XFIND.BAD 保持一致：这些一律不能当成正片给用户听
BAD = re.compile(r"(伴奏|纯音乐|instrumental|karaoke|消音|DJ版|dj ?版|remix|加速|慢速|"
                 r"降调|升调|翻唱|cover|片段|铃声|试听|串烧|0\.\d+x|1\.\d+x|live|现场|"
                 r"演唱会|完整版|搞笑|纯享|舞曲|广场舞|钢琴版|吉他版|口琴|笛子|萨克斯|"
                 r"八音盒|教学|清唱|demo|demo版|试音)", re.I)

MAX_MISS = 4          # 连续失败这么多轮就不再重试


def get_json(url, ref="https://music.163.com/", ms=15, tries=3):
    """★ 2026-10-30 实测：同一 IP 并发稍密，网易搜索接口就回 **code=406**（风控），
       且不是 httperror 而是业务码 —— 旧代码直接当「搜不到」，于是一口气把
       3475 首判定为 no-result（实测串行重试立刻恢复 200）。
       这里：406/空结果一律退避重试（2s / 5s），**不算失败**。"""
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers=dict(UA, Referer=ref))
            d = json.loads(OP.open(req, timeout=ms).read().decode("utf-8", "replace"))
        except Exception:
            time.sleep(1.5 + 2 * i)
            continue
        code = d.get("code")
        if d.get("result") or code == 200:
            return d
        if code in (406, -462, 301) or i < tries - 1:
            time.sleep(2 + 3 * i)
            continue
        return d
    return None


def probe(sid):
    """真打直链：必须真是音频"""
    try:
        req = urllib.request.Request(OUTER % sid, headers=dict(UA, Range="bytes=0-2047"))
        r = OP.open(req, timeout=15)
        ct = (r.headers.get("Content-Type") or "").lower()
        r.read(1024)
        if r.status in (200, 206) and ct.startswith("audio"):
            return True, "ok"
        return False, "ctype=%s/%s" % (r.status, ct[:20])
    except Exception as e:
        return False, repr(e)[:40]


def resolve_one(c):
    """返回 (cid, dur) 或 None —— 三关校验 + 真测全过才算"""
    q = (c["t"] + " " + (c["s"] or "")).strip()
    d = get_json(SEARCH % urllib.parse.quote(q))
    arr = (((d or {}).get("result") or {}).get("songs") or [])
    if not arr:
        return None, "no-result"
    bt = E.norm(E.strip(c["t"]))
    ba = E.norm(c["s"] or "")
    best, bsc = None, 0
    for x in arr:
        nm = x.get("name") or ""
        if BAD.search(nm):
            continue
        ct = E.norm(E.strip(nm))
        # ① 歌名
        if ct == bt:
            sc = 100
        elif ct and bt and (ct in bt or bt in ct):
            sc = 70
        else:
            continue
        # ② 歌手（必须沾边，杜绝同名不同人）
        an = "/".join((z.get("name") or "") for z in (x.get("artists") or []))
        ca = E.norm(an)
        if ba and ca:
            if ca == ba or ca in ba or ba in ca:
                sc += 20
            else:
                continue
        # ③ 时长
        dur = int(x.get("duration") or 0) / 1000.0
        cd = c.get("dur") or 0
        if cd and dur:
            if abs(dur - cd) <= 3:
                sc += 15
            elif abs(dur - cd) <= 8:
                sc += 5
            else:
                continue
        if sc > bsc:
            bsc, best = sc, (str(x.get("id") or ""), dur)
    if not best or not best[0]:
        return None, "no-match"
    ok, why = probe(best[0])
    if not ok:
        return None, "dead:" + why
    return best, "ok"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry", action="store_true")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--budget", type=int, default=1500)
    ap.add_argument("--limit", type=int, default=0)
    a = ap.parse_args()

    st = json.load(open(STREAM, encoding="utf-8"))
    omap = st.get("map") or {}
    before = len(omap)

    miss = {}
    if os.path.exists(MISSF):
        try:
            miss = json.load(open(MISSF, encoding="utf-8"))
        except Exception:
            miss = {}

    cur = E.curated()
    todo = [(k, c) for k, c in cur.items() if k not in omap]
    # 连败太多的暂时不碰（每轮给一次机会：只在是本轮新加入时才碰）
    todo = [(k, c) for k, c in todo if int(miss.get(k) or 0) < MAX_MISS]
    random.Random(7).shuffle(todo)
    if a.limit:
        todo = todo[:a.limit]
    print("缺源 %d 首，本轮处理 %d 首（连败≥%d 已冻结 %d 首）"
          % (len(cur) - before, len(todo), MAX_MISS, sum(1 for v in miss.values() if v >= MAX_MISS)))
    if not todo:
        print("没有缺源曲目，收工")
        return 0

    t0 = time.time()
    res, why = {}, {}
    with cf.ThreadPoolExecutor(max_workers=a.workers) as ex:
        futs = {ex.submit(resolve_one, c): k for k, c in todo}
        for n, fu in enumerate(cf.as_completed(futs), 1):
            k = futs[fu]
            try:
                r, w = fu.result()
            except Exception as e:
                r, w = None, "err:" + repr(e)[:40]
            if r:
                res[k] = r
            else:
                why[k] = w
            if n % 100 == 0:
                print("  %d/%d ｜%.0fs ｜命中 %d" % (n, len(todo), time.time() - t0, len(res)))
            if time.time() - t0 > a.budget:
                print("\n⏳ 到预算 %ds，剩余下轮续跑" % a.budget)
                break

    print("\n=== 本轮结论 ===")
    print("  补到并真测通过 %d 首" % len(res))
    print("  未命中       %d 首" % len(why))
    for w, n in sorted(((w, list(why.values()).count(w)) for w in set(why.values())),
                       key=lambda x: -x[1])[:6]:
        print("      %-24s %d" % (w, n))

    if a.dry:
        print("--dry：不写盘")
        return 0

    for k, r in res.items():
        c = cur[k]
        omap[k] = {"wy": r[0], "n": c["t"], "a": c["s"],
                   "dur": int(r[1] or c.get("dur") or 0), "native": 1}
        miss.pop(k, None)
    for k in why:
        miss[k] = int(miss.get(k) or 0) + 1

    srcs = {}
    for v in omap.values():
        s = v.get("src") or ("wy" if v.get("wy") else "?")
        srcs[s] = srcs.get(s, 0) + 1
    st["count"] = len(cur)
    st["map"] = omap
    st["srcs"] = srcs
    st["hit"] = sum(1 for k in cur if k in omap)
    st["rate"] = round(100.0 * st["hit"] / max(1, len(cur)), 1)
    st["note"] = (st.get("note") or "") + (
        " ★ 2026-10-30 起 tools/resolve_missing.py 服务端批量翻译+真测落库。"
        "明确查不到的记在 data/resolve_miss.json，不重复骚扰。")
    json.dump(st, open(STREAM, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    json.dump(miss, open(MISSF, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    hit = st["hit"]
    print("💾 streams.json：%d → %d 首｜端上可见曲目可播率 %.1f%%"
          % (before, len(omap), 100.0 * hit / max(1, len(cur))))
    return 0


if __name__ == "__main__":
    sys.exit(main())
