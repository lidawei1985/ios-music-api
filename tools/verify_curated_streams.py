# -*- coding: utf-8 -*-
r"""给「端上真会放到的歌」把音源补满，并且**逐条真测**。

=========================== 为什么要写它 ============================
2026-10-30 主人指令：「伴奏 翻唱 还有没有资源直接跳过……采集器歌曲没有增量质量也没有保证」。

体检结果（本机实测）：
    端上可见曲目（library/charts/categories/hero）共 5105 首
      · 有**服务端验证过**的音源(streams.json) 只有 1623 首 = 31.8%
      · 歌词 94.1%，评论 54.2%
也就是说用户点开 2/3 的歌，如果不靠端上的本地/在线补源，就是「没资源跳过」。

本脚本做两件事，把「.Database config 的 31.8%」往上推：
  ① 用本机 7.6 万首曲库索引，把缺源曲目翻译成真实 id（零猜测，歌名+歌手双锁）
  ② **逐条真测**：对翻译出来的 wy  id 打 outer/url 直链，要求回 200/206 且
     Content-Type 是 audio/，否则**宁缺毋滥不写入**（这正是旧��器「质量没保证」的根）

用法：
    python tools/verify_curated_streams.py --dry
    python tools/verify_curated_streams.py --workers 12
"""
import argparse
import concurrent.futures as cf
import json
import os
import sys
import time
import urllib.request

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import enrich_streams as E                      # noqa: E402  （复用同一套匹配口径）

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DATA = os.path.join(ROOT, "data")
STREAM = os.path.join(DATA, "streams.json")

UA = {"User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
                    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 "
                    "Mobile/15E148 Safari/604.1"}
OP = urllib.request.build_opener(urllib.request.ProxyHandler({}))
OUTER = "https://music.163.com/song/media/outer/url?id=%s.mp3"


def probe_wy(sid):
    """真打一次直链：能响才算数。回 (True/False, 说明)"""
    try:
        req = urllib.request.Request(OUTER % sid, headers=dict(UA, Range="bytes=0-2047"))
        r = OP.open(req, timeout=15)
        ct = (r.headers.get("Content-Type") or "").lower()
        r.read(1024)
        if r.status in (200, 206) and ct.startswith("audio"):
            return True, ct
        return False, "ctype=%s/%s" % (r.status, ct[:20])
    except Exception as e:
        return False, repr(e)[:50]


def build_index():
    rows = E.load_catalog()
    idx = {}
    for r in rows:
        idx.setdefault(r[0], []).append(r)
        st = E.strip(r[4])
        if st and st != r[0]:
            idx.setdefault(st, []).append(r)
    return idx


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry", action="store_true")
    ap.add_argument("--workers", type=int, default=12)
    ap.add_argument("--budget", type=int, default=1800)
    a = ap.parse_args()

    st = json.load(open(STREAM, encoding="utf-8"))
    omap = st.get("map") or {}
    before = len(omap)

    cur = E.curated()
    todo = []
    for k, c in cur.items():
        if k not in omap:
            todo.append((k, c))
    print("端上可见 %d 首，已带音源 %d 首，待补 %d 首" % (len(cur), len(omap), len(todo)))

    idx = build_index()
    t0 = time.time()
    plan = []
    skip = 0
    for k, c in todo:
        m = E.match(idx, c["t"], c["s"])
        if not m:
            skip += 1
            continue
        cand_id, cand_src = m[2], (m[3] or "wy")
        if not cand_id:
            skip += 1
            continue
        plan.append((k, c, cand_id, cand_src))
    print("曲库索引翻译成功 %d 首，译不出 %d 首" % (len(plan), skip))

    if not plan:
        print("没有可补的")
        return 0

    print("逐条真测直链（并发 %d）…" % a.workers)
    okmap, bad = {}, []
    with cf.ThreadPoolExecutor(max_workers=a.workers) as ex:
        futs = {ex.submit(probe_wy_e, it): it for it in plan}
        for n, fu in enumerate(cf.as_completed(futs), 1):
            it = futs[fu]
            try:
                good, why = fu.result()
            except Exception as e:
                good, why = False, repr(e)[:50]
            if good:
                okmap[it[0]] = it
            else:
                bad.append((it[2], it[1].get("t", "")[:20], why))
            if n % 200 == 0:
                print("  %d/%d ｜%.0fs ｜通过 %d" % (n, len(plan), time.time() - t0, len(okmap)))
            if time.time() - t0 > a.budget:
                print("\n⏳ 到预算 %ds，剩余下轮续跑" % a.budget)
                break

    print("\n=== 本轮结论 ===")
    print("  实测通过 %d 首 ｜不通过 %d 首（不写入）" % (len(okmap), len(bad)))
    for r in bad[:15]:
        print("    ✗ %s %s （%s）" % r)

    if a.dry:
        print("--dry：不写盘")
        return 0

    added = 0
    for k, it in okmap.items():
        _, c, cid, csrc = it
        if k in omap:
            continue
        rec = {"n": c["t"], "a": c["s"], "dur": int(c.get("dur") or 0)}
        if csrc and csrc != "wy":
            rec[csrc] = cid
            rec["src"] = csrc
        else:
            rec["wy"] = cid
        omap[k] = rec
        added += 1

    st["count"] = before + added
    st["map"] = omap
    srcs = {}
    for v in omap.values():
        for s in ("wy", "kw", "migu", "bili"):
            if v.get(s):
                srcs[s] = srcs.get(s, 0) + 1
    st["srcs"] = srcs
    st["note"] = (st.get("note") or "") + (
        " ★ 2026-10-30 起 tools/verify_curated_streams.py 对「端上可见曲目」逐条真测补源，"
        "打不通的一律不写入。")
    json.dump(st, open(STREAM, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print("💾 已写回 streams.json：%d → %d 首（+%d）" % (before, len(omap), added))
    return 0


# 真测包装（放在 module 层，ThreadPoolExecutor 才能 pickle 到）
def probe_wy_e(it):
    _, c, cid, csrc = it
    if csrc and csrc != "wy":
        return False, "skip:%s" % csrc
    return probe_wy(cid)


if __name__ == "__main__":
    sys.exit(main())
