#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
build.py —— 源池维护流水线（无人值守，跑在 GitHub Actions 上）
每轮：
  1) 体检：对每个源跑探针曲（搜索→取链→Range GET 验证），记录成功/延迟/音质
  2) 打分：score = 成功率/延迟/音质/稳定性 加权
  3) 分档：active / degraded / needs_auth / quarantined（原因写进 notes）
  4) 自动补给：从候选池（未启用或降权的源）小批量试跑，达标即升为 active
  5) 沉淀「自有歌库」：把通过验证的曲目及其各源 id 固化进 data/library.json（越跑越全）
  6) 产出 data/pool.json + data/charts.json（App 只认这两个 + library）

零第三方依赖（纯标准库）。
"""
import json, os, statistics, sys, time, traceback
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import adapters as A

DATA = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
ROUND_KEEP = 40          # 历史保留轮数
LIB_MAX = int(os.environ.get("LIB_MAX") or "2000")        # 自有歌库上限
LIB_BUDGET_S = int(os.environ.get("LIB_BUDGET_S") or "600")  # 建库时间预算（秒）

# 探针曲：覆盖 热门 / 老歌 / 不同歌手 / 常被标 VIP 的曲
PROBES = [
    ("稻香", "周杰伦"), ("晴天", "周杰伦"), ("告白气球", "周杰伦"), ("夜曲", "周杰伦"),
    ("小苹果", "筷子兄弟"), ("海阔天空", "BEYOND"), ("成都", "赵雷"), ("平凡之路", "朴树"),
    ("演员", "薛之谦"), ("起风了", "买辣椒也用券"), ("光年之外", "邓紫棋"), ("消愁", "毛不易"),
    ("孤勇者", "陈奕迅"), ("罗刹海市", "刀郎"), ("漠河舞厅", "柳爽"), ("星辰大海", "黄霄雲"),
    ("后来", "刘若英"), ("爱你", "王心凌"), ("月亮代表我的心", "邓丽君"), ("一生所爱", "卢冠廷"),
]
# 允许环境变量裁剪探针数（本地快测 / 云端按预算调）；空串视为缺省
_pl = (os.environ.get("PROBES_LIMIT") or "").strip()
PROBES = PROBES[:int(_pl)] if _pl.isdigit() else PROBES
# 允许环境变量裁剪参与体检的源
_sids = [x for x in (os.environ.get("SRC_IDS") or "").split(",") if x.strip()]
SRC_IDS = _sids or ["migu", "bili", "wy", "qq", "kg"]

QUALITY_SCORE = {"flac": 1.0, "SQ": 1.0, "320kbps": 1.0, "320k": 0.9, "HQ": 0.85,
                 "211kbps": 0.85, "192kbps": 0.8, "mp3": 0.7, "m4a": 0.7,
                 "167kbps": 0.7, "132kbps": 0.65, "128k": 0.6, "64kbps": 0.45, "PQ": 0.6}

# 源角色：hifi=正版曲库（优先，含无损/HQ）；backstop=视频兜底（全覆盖但为二创/转码）；auth=需登录
ROLE = {"migu": "hifi", "wy": "hifi", "bili": "backstop", "qq": "auth", "kg": "auth"}


def load(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def save(path, obj, indent=1):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=indent)


# ------------------------------------------------------------------ 1) 体检
def probe_song(src_id, src, title, singer):
    rec = {"title": title, "singer": singer, "src": src_id, "search_n": 0,
           "matched": False, "ok": False, "quality": None, "ms": 0, "note": "", "tries": 0}
    t0 = time.time()
    try:
        items = src.search(title, singer)
        rec["search_n"] = len(items)
        if not items:
            rec["note"] = "搜索无结果"
            return rec
        cands = A.pick_all(items, title, singer, lambda x: x["title"], lambda x: x["singer"], limit=3)
        if not cands:
            rec["note"] = f"无匹配(共{len(items)}条)"
            return rec
        rec["matched"] = True
        t1 = time.time()
        want = A.norm(singer)
        for i, it in enumerate(cands):          # 同源多候选回退：第一候选失败顺延
            if i > 0 and not getattr(src, "VIDEO", False):
                # 曲库源：回退候选必须同歌手，避免"周杰伦的晴天"放到"张信哲的晴天"
                got = A.norm(it.get("singer", ""))
                if want and not (want in got or got in want):
                    continue
            rec["tries"] = i + 1
            src.last_reason = ""
            r = src.resolve(it)
            if r:
                rec["ok"] = True
                rec["quality"] = r.get("quality")
                rec["note"] = r.get("note", "")
                rec["url"] = r.get("url", "")[:300]
                rec["ids"] = it.get("raw")
                rec["item"] = {k: it.get(k) for k in ("title", "singer", "album", "cover")}
                rec["ms"] = int((time.time() - t1) * 1000)
                break
            rec["note"] = getattr(src, "last_reason", "") or "取链失败"
        rec["ms"] = int((time.time() - t1) * 1000)
        if not rec["ok"]:
            rec["picked"] = f"{cands[0]['title']} / {cands[0]['singer']}"
    except Exception as e:
        rec["note"] = f"异常 {type(e).__name__}: {e}"
    finally:
        rec["total_ms"] = int((time.time() - t0) * 1000)
    return rec


def health_one(src_id, src):
    recs = []
    with ThreadPoolExecutor(max_workers=6) as ex:
        futs = [ex.submit(probe_song, src_id, src, t, s) for t, s in PROBES]
        for f in as_completed(futs):
            recs.append(f.result())
    okr = [r for r in recs if r["ok"]]
    lat = [r["ms"] for r in okr if r.get("ms")]
    qs = [r.get("quality") for r in okr if r.get("quality")]
    qscore = statistics.mean([QUALITY_SCORE.get(q, 0.6) for q in qs]) if qs else 0.0
    return {"id": src_id, "name": src.name, "kind": src.kind, "homepage": src.homepage,
            "role": ROLE.get(src_id, "backstop"),
            "chain_ok": len(okr) / len(recs) if recs else 0.0,
        "search_ok": sum(1 for r in recs if r["search_n"] > 0) / len(recs) if recs else 0.0,
        "match_ok": sum(1 for r in recs if r["matched"]) / len(recs) if recs else 0.0,
        "latency_ms": int(statistics.median(lat)) if lat else None,
        "quality_score": round(qscore, 3),
        "ok_count": len(okr), "total": len(recs),
        "note": next((r["note"] for r in reversed(recs) if not r["ok"] and r["note"]), ""),
        "details": [{"t": r["title"], "s": r["singer"], "ok": r["ok"], "q": r.get("quality"),
                     "ms": r.get("ms"), "note": r["note"]} for r in sorted(recs, key=lambda x: x["title"])],
        "hits": okr,
    }


def score_of(h, prev_score=None):
    lat = h["latency_ms"] or 4000
    lat_score = max(0.0, min(1.0, 1 - (lat - 300) / 3500))
    stability = (prev_score / 100.0) if prev_score else 0.6
    s = 100 * (0.50 * h["chain_ok"] + 0.20 * lat_score + 0.15 * h["quality_score"] + 0.15 * stability)
    return round(s, 1)


def status_of(h, score):
    if h["chain_ok"] >= 0.5:
        return "active" if score >= 55 else "degraded"
    if h["search_ok"] >= 0.6 and h["chain_ok"] < 0.2:
        return "needs_auth" if ("登录" in h["note"] or "账号" in h["note"] or "token" in h["note"]) else "quarantined"
    return "quarantined"


# ------------------------------------------------------------------ 2) 自有歌库
def build_library(old_lib, chart_songs, hits):
    """沉淀：探针命中 + 榜单曲目 的 各源 id 映射，越跑越全"""
    t0 = time.time()
    songs = {}   # key = norm(title)+'|'+norm(singer)
    for e in old_lib.get("songs", []):
        k = A.norm(e.get("t", "")) + "|" + A.norm(e.get("s", ""))
        songs[k] = dict(e)

    def upsert(title, singer, album="", cover="", dur=None, ids=None, src=None, verified=False):
        k = A.norm(title) + "|" + A.norm(singer)
        e = songs.setdefault(k, {"t": title, "s": singer, "a": album, "cov": cover,
                                 "dur": dur, "ids": {}, "first": time.strftime("%Y-%m-%d"),
                                 "seen": 0, "sources": []})
        e["seen"] = e.get("seen", 0) + 1
        e["last"] = time.strftime("%Y-%m-%d")
        if album and not e.get("a"): e["a"] = album
        if cover and not e.get("cov"): e["cov"] = cover
        if dur and not e.get("dur"): e["dur"] = dur
        if ids:
            e["ids"][src] = ids
            if verified and src not in e["sources"]:
                e["sources"].append(src)
        return e

    # 2a. 体检命中项（带真实可播证据）
    for h in hits:
        it = h.get("item") or {}
        upsert(it.get("title", h["title"]), it.get("singer", h["singer"]), it.get("album", ""),
               it.get("cover", ""), None, h.get("ids"), h["src"], verified=True)

    # 2b. 榜单曲目入自有歌库（元数据沉淀，含封面）
    before = len(songs)
    added = 0
    for s in chart_songs:
        if len(songs) >= LIB_MAX or time.time() - t0 > LIB_BUDGET_S:
            break
        upsert(s["title"], s["singer"], s.get("album", ""), s.get("cover", ""), s.get("duration"),
               {"mid": s.get("mid")} if s.get("mid") else None, "qq", verified=False)
        added += 1

    return {"updated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "count": len(songs), "chart_scanned": added, "chart_new": len(songs) - before,
            "songs": sorted(songs.values(), key=lambda x: (-x.get("seen", 0), x.get("t", "")))}


# ------------------------------------------------------------------ 2b) 歌手头像库
def build_artists(old, songs, hits):
    """歌手头像：从榜单/命中的歌手 mid 生成我们自己的头像地址（T001R300x300）"""
    arts = {}
    for a in old.get("artists", []):
        if a.get("n"):
            arts[A.norm(a["n"])] = dict(a)
    for s in songs:
        for g in (s.get("singers") or []):
            n, mid = (g.get("name") or "").strip(), g.get("mid")
            if not n or not mid:
                continue
            k = A.norm(n)
            e = arts.setdefault(k, {"n": n, "seen": 0})
            e["seen"] = e.get("seen", 0) + 1
            if not e.get("mid"):
                e["mid"] = mid
    for h in hits:
        s = (h.get("item") or {}).get("singer") or h.get("singer") or ""
        for n in [x.strip() for x in s.replace("、", "/").replace("&", "/").split("/") if x.strip()]:
            k = A.norm(n)
            e = arts.setdefault(k, {"n": n, "seen": 0})
            e["seen"] = e.get("seen", 0) + 1
    for e in arts.values():
        if e.get("mid"):
            e["av"] = A.qq_singer_avatar(e["mid"])
    return {"updated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "count": sum(1 for e in arts.values() if e.get("av")),
            "artists": sorted(arts.values(), key=lambda x: (-x.get("seen", 0), x.get("n", "")))}


# ------------------------------------------------------------------ 2c) MV / 演唱会
MV_BUDGET_S = int(os.environ.get("MV_BUDGET_S") or "300")
# 演唱会/现场 固定关键词（大牌优先，人工精选但由机器执行）
MV_LIVE_KEYWORDS = ["周杰伦 演唱会 官方", "五月天 演唱会 官方", "陈奕迅 演唱会 官方",
                    "邓紫棋 演唱会 官方", "林俊杰 演唱会 官方", "TFBOYS 演唱会 官方"]


def build_mv(chart_songs):
    """MV/演唱会类别：B站音乐分区(tid=3)搜索 → 元数据入库 → 抽样验证真能出 1080P 流"""
    b = A.Bili()
    t0 = time.time()
    st = {"budget": 0, "verified": 0, "q": {}}

    def search_all(keywords, limit):
        seen, items = set(), []
        for kw in keywords:
            if time.time() - t0 > MV_BUDGET_S:
                break
            try:
                res = b.search_mv(kw, limit=limit)
            except Exception:
                continue
            for it in res:
                bv = it["raw"]["bvid"]
                if bv in seen:
                    continue
                seen.add(bv)
                items.append(it)
        return items

    def verify(items, n=3):
        for it in items[:n]:
            if st["budget"] >= 12 or time.time() - t0 > MV_BUDGET_S:
                return
            st["budget"] += 1
            try:
                r = b.resolve_mv(it)
            except Exception:
                r = None
            if r:
                it["q"] = r["quality"]
                it["wh"] = f"{r['width']}x{r['height']}"
                st["verified"] += 1
                st["q"][r["quality"]] = st["q"].get(r["quality"], 0) + 1

    def pack(name, keywords, per_kw, limit=12, verify_n=3):
        items = search_all(keywords, limit=per_kw)
        verify(items, verify_n)
        items = items[:limit]
        flat = [{"t": x["title"], "a": x["author"], "cov": x["cover"], "dur": x["dur"],
                 "play": x["play"], "bv": x["raw"]["bvid"], "q": x.get("q", ""), "wh": x.get("wh", "")}
                for x in items]
        print(f"  MV[{name}] {len(flat)} 条")
        return {"name": name, "count": len(flat), "items": flat}

    cols = []
    if chart_songs:
        cols.append(pack("热门 MV",
                         [f"{s['title']} {s['singer'].split('/')[0]} MV" for s in chart_songs[:8]],
                         per_kw=4, limit=14, verify_n=4))
        cols.append(pack("现场 LIVE",
                         [f"{s['title']} {s['singer'].split('/')[0]} 现场 live" for s in chart_songs[8:16]],
                         per_kw=4, limit=12, verify_n=2))
    cols.append(pack("演唱会现场", MV_LIVE_KEYWORDS, per_kw=5, limit=16, verify_n=4))

    flat = [x for c in cols for x in c["items"]]
    return {"updated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "collections": cols, "count": len(flat),
            "verified": st["verified"], "verified_quality": st["q"],
            "note": "取流关键: fnval=4048&fourk=1&qn=127&try_look=1（否则匿名只回 480P）"}


# ------------------------------------------------------------------ main
def main():
    os.makedirs(DATA, exist_ok=True)
    prev = load(os.path.join(DATA, "pool.json"), {})
    prev_src = {s["id"]: s for s in prev.get("sources", [])}
    rnd = int(prev.get("round", 0)) + 1
    srcs = A.instantiate()

    print(f"===== 第 {rnd} 轮源池维护 =====")
    results, all_hits = [], []
    for sid in SRC_IDS:
        s = srcs[sid]
        try:
            h = health_one(sid, s)
        except Exception:
            print(traceback.format_exc())
            continue
        p = prev_src.get(sid, {})
        sc = score_of(h, p.get("score"))
        st = status_of(h, sc)
        hist = (p.get("history") or []) + [[int(time.time()), sc]]
        h.update({"score": sc, "status": st, "history": hist[-ROUND_KEEP:]})
        all_hits.extend(h.pop("hits"))
        print(f"  {h['name']:<8} {st:<11} score={sc:<6} 取链 {h['ok_count']}/{h['total']}  "
              f"搜索命中 {h['match_ok']*100:.0f}%  延迟 {h['latency_ms']}ms  "
              f"音质分 {h['quality_score']}  {h['note'][:40]}")
        results.append(h)

    # 自动补给：把降权的源重新排队（本轮已自动重测）；额外记录补给日志
    replenish = []
    for h in results:
        p = prev_src.get(h["id"], {})
        if p.get("status") in ("quarantined", "needs_auth", "degraded") and h["status"] == "active":
            replenish.append({"source": h["id"], "action": "auto-promoted", "score": h["score"]})
        if p.get("status") == "active" and h["status"] != "active":
            replenish.append({"source": h["id"], "action": "auto-demoted", "score": h["score"]})

    # 榜单（我们自己的规范化）
    charts, chart_songs = [], []
    for topid, name, lim in A.QQ_TOPLISTS:
        try:
            c = A.qq_toplist(topid, name, lim)
            charts.append(c); chart_songs.extend(c["songs"])
            print(f"  榜单 {name}: {c['count']} 首")
        except Exception as e:
            print(f"  榜单 {name} 失败 {e}")
    hotkeys = []
    try:
        hotkeys = A.qq_hotkeys()
    except Exception:
        pass

    lib = build_library(load(os.path.join(DATA, "library.json"), {}), chart_songs[:LIB_MAX], all_hits)
    arts = build_artists(load(os.path.join(DATA, "artists.json"), {}), chart_songs[:LIB_MAX], all_hits)
    mv = {}
    if os.environ.get("WITH_MV", "1") != "0":
        print("  —— MV / 演唱会 ——")
        try:
            mv = build_mv(chart_songs[:LIB_MAX])
        except Exception:
            print(traceback.format_exc())

    active = sorted([h for h in results if h["status"] == "active"], key=lambda x: -x["score"])
    # 试源顺序：高保真曲库源（按分）→ 视频兜底源（按分）。App 依次试，首个可播即播。
    hifi = [h["id"] for h in active if h["role"] == "hifi"]
    back = [h["id"] for h in active if h["role"] != "hifi"]
    order = hifi + back
    # 云端机房出口被地域风控时探测会全灭；此时回落到本地家宽实测过的默认序（设备端解析用）。
    if not order:
        order = ["wy", "migu", "bili"]
        print("WARN: 云端探测无 active 源（地域风控），order 回落默认序 " + " -> ".join(order))
    save(os.path.join(DATA, "pool.json"), {
        "updated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "round": rnd, "probes": [{"title": t, "singer": s} for t, s in PROBES],
        "order": order,                                   # App 按这个顺序试源（优质优先）
        "order_source": "probe" if hifi or back else "fallback",
        "hifi": hifi, "backstop": back,
        "sources": results, "replenish_log": replenish,
    })
    save(os.path.join(DATA, "charts.json"), {
        "updated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "hotkeys": hotkeys, "lists": charts,
    })
    save(os.path.join(DATA, "library.json"), lib, indent=None)
    save(os.path.join(DATA, "artists.json"), arts, indent=None)
    if mv:
        save(os.path.join(DATA, "mv.json"), mv, indent=None)

    summary = ", ".join("%s(%s/%s)" % (h["id"], h["role"], h["score"]) for h in active)
    print("\n试源顺序: " + " → ".join(order) + "   [高保真 " + summary + "]")
    print("自有歌库: %s 首（榜单扫描 %s，新入库 %s）；歌手头像 %s 个；补给日志 %s"
          % (lib["count"], lib["chart_scanned"], lib["chart_new"], arts["count"], replenish))
    if mv:
        print("MV/演唱会: %s 条（%s 个合集，实测取流通过 %s 条 %s）"
              % (mv["count"], len(mv["collections"]), mv["verified"], mv["verified_quality"]))
    print("已写 data/pool.json / data/charts.json / data/library.json / data/artists.json"
          + (" / data/mv.json" if mv else ""))


if __name__ == "__main__":
    main()
