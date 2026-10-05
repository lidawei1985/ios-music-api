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

    # 2c. 海报主色沉淀（增量：只补缺 hue 的歌，缓存进库越跑越全；App 播放谁就用谁的主色）
    hue_done = 0
    need = [e for e in songs.values() if e.get("cov") and e.get("hue") is None]
    if need and HUE_BUDGET_S > 0:
        from concurrent.futures import ThreadPoolExecutor as _TPE
        tH = time.time()
        need.sort(key=lambda x: -x.get("seen", 0))          # 常听的先配色
        with _TPE(max_workers=6) as ex:
            futs = {ex.submit(dominant_hue, hd_cover(e["cov"])): e for e in need}
            for fu in as_completed(futs):
                if time.time() - tH > HUE_BUDGET_S:
                    break
                e = futs[fu]
                try:
                    h = fu.result()
                except Exception:
                    h = None
                if h is not None:
                    e["hue"] = h
                    hue_done += 1
    hue_total = sum(1 for e in songs.values() if e.get("hue") is not None)

    return {"updated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "count": len(songs), "chart_scanned": added, "chart_new": len(songs) - before,
            "hue_new": hue_done, "hue_total": hue_total,
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


# ------------------------------------------------------------------ 2d) 主视觉轮播（高清海报 + 预计算主色）
HERO_N = int(os.environ.get("HERO_N") or "24")               # 主视觉海报数（硬要求 ≥20）
HERO_BUDGET_S = int(os.environ.get("HERO_BUDGET_S") or "300")
HUE_BUDGET_S = int(os.environ.get("HUE_BUDGET_S") or "240")  # 歌库主色增量预算（秒/轮，越跑越全）


def hd_cover(url):
    """QQ 封面升到 800×800 高清（实测 R1080 不存在、R800 可用；约 128KB/张）"""
    if not url:
        return ""
    u = url.replace("/500x500/", "/800x800/").replace("/300x300/", "/800x800/")
    for tag in ("T002R500x500M000", "T002R300x300M000", "T002R150x150M000"):
        u = u.replace(tag, "T002R800x800M000")
    return u


def dominant_hue(url):
    """取封面主色相（0-359），供 App 背景/主题跟随变色。阈值由 24 张真实海报实测标定：
    彩色像素占比 <12% 或 最佳 45° 色相窗占全图 <6% → 判为「无可信主色」返回 None
    （周杰伦《我不难过》窗口 19.6%、Dear You 75.2% 通过；《异想天开》2.1%、《甲乙丙丁》0.1% 剔除）。
    色相一律取自「窗内像素平均色」而非桶 key，保证与真实观感一致。"""
    try:
        import io as _io, colorsys, urllib.request
        from PIL import Image
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0",
                                                   "Referer": "https://y.qq.com/"})
        raw = urllib.request.urlopen(req, timeout=12).read()
        im = Image.open(_io.BytesIO(raw)).convert("RGB").resize((36, 36))
        px = list(im.getdata())
        total = len(px)
        hist = [0.0] * 24
        srgb = [[0, 0, 0, 0] for _ in range(24)]
        valid = 0
        for r, g, b in px:
            h, s, v = colorsys.rgb_to_hsv(r / 255, g / 255, b / 255)
            if s < 0.16 or v < 0.14 or v > 0.95:      # 近灰/近黑/近白不计
                continue
            i = int(h * 24) % 24
            hist[i] += s                              # 按饱和度加权（灰调自动降权）
            a = srgb[i]; a[0] += r; a[1] += g; a[2] += b; a[3] += 1
            valid += 1
        if valid == 0 or valid / total < 0.12:
            return None
        bi, bw = 0, -1.0
        for i in range(24):
            w = hist[i] + hist[(i - 1) % 24] + hist[(i + 1) % 24]   # 3 桶环形窗(45°)抗噪
            if w > bw:
                bi, bw = i, w
        if bw / total < 0.06:
            return None
        rs = gs = bs = c = 0
        for k in ((bi - 1) % 24, bi, (bi + 1) % 24):
            a = srgb[k]; rs += a[0]; gs += a[1]; bs += a[2]; c += a[3]
        if not c:
            return None
        h, s, v = colorsys.rgb_to_hsv(rs / c / 255, gs / c / 255, bs / c / 255)
        return int(h * 360) % 360
    except Exception:
        return None


def build_hero(charts):
    """主视觉：跨榜单轮流取「有专辑封面」的头部曲目 → 800×800 高清海报 + 预计算主色"""
    t0 = time.time()
    seen_album, items = set(), []
    cols = [c.get("songs") or [] for c in charts]
    depth = 0
    while len(items) < HERO_N and cols and depth < 80:
        for ci, songs in enumerate(cols):
            if len(items) >= HERO_N:
                break
            if depth >= len(songs):
                continue
            s = songs[depth]
            am = (s.get("albummid") or "").strip()
            cov = (s.get("cover") or "").strip()
            if not cov or not am or am in seen_album:
                continue
            seen_album.add(am)
            items.append({"t": s.get("title", ""), "s": s.get("singer", ""),
                          "a": s.get("album", ""), "cov": hd_cover(cov),
                          "dur": s.get("duration") or 0, "mid": s.get("mid", ""),
                          "albummid": am, "chart": charts[ci].get("name", ""),
                          "rank": s.get("rank", ""), "hue": None})
        depth += 1

    ok = 0
    for it in items:
        if time.time() - t0 > HERO_BUDGET_S:
            break
        h = dominant_hue(it["cov"])
        if h is not None:
            it["hue"] = h
            ok += 1
    print("  主视觉 %s 张高清海报（%s 张已取主色）" % (len(items), ok))
    return {"updated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "count": len(items), "hue_ok": ok, "items": items,
            "note": "海报 800×800 高清；hue=该海报主色相，App 背景/主题跟随变色（缺失按标题哈希兜底）"}


# ------------------------------------------------------------------ 2e) 歌库分类（照大牌逻辑：语种/流派/主题/心情/场景）
CAT_BUDGET_S = int(os.environ.get("CAT_BUDGET_S") or "420")   # 分类抓取时间预算
CAT_PLAYLISTS = int(os.environ.get("CAT_PLAYLISTS") or "2")   # 每分类取几个歌单
CAT_MAX_SONGS = int(os.environ.get("CAT_MAX_SONGS") or "60")  # 每分类最多歌曲数


def build_categories():
    """官方分类体系（语种9/流派16/主题17/心情9/场景13）→ 每类取热门歌单 → 规范化歌曲。
    数据全部落成我们自己的 data/categories.json，App 离线可浏览（含 DJ/伤感/开车/睡前 等直达分类）。"""
    from concurrent.futures import ThreadPoolExecutor
    t0 = time.time()
    groups = A.qq_diss_tags()
    jobs = [(g["group"], c["name"], c["id"]) for g in groups for c in g["cats"]]
    print("  分类标签 %s 组 %s 类" % (len(groups), len(jobs)))

    def one(group, cname, cid):
        try:
            pls = A.qq_diss_list(cid, sort=3, n=CAT_PLAYLISTS)
        except Exception:
            return group, cname, []
        seen, songs = set(), []
        for p in pls:
            if len(songs) >= CAT_MAX_SONGS:
                break
            try:
                ss = A.qq_diss_songs(p["dissid"], 40)
            except Exception:
                continue
            for s in ss:
                k = (A.norm(s["title"]), A.norm(s["singer"]))
                if not k[0] or k in seen:
                    continue
                seen.add(k)
                songs.append({"t": s["title"], "s": s["singer"], "a": s["album"],
                              "cov": s["cover"], "dur": s["duration"] or 0,
                              "mid": s.get("mid") or "", "albummid": s.get("albummid") or ""})
                if len(songs) >= CAT_MAX_SONGS:
                    break
        return group, cname, songs

    out, done = {g["group"]: {} for g in groups}, 0
    with ThreadPoolExecutor(max_workers=6) as ex:
        futs = {ex.submit(one, g, c, cid): (g, c) for g, c, cid in jobs}
        for fu in as_completed(futs):
            g, c = futs[fu]
            try:
                _, cname, songs = fu.result()
            except Exception:
                continue
            done += 1
            if songs:
                out[g][c] = songs
            if done % 16 == 0:
                print("    分类进度 %s/%s  %.0fs" % (done, len(jobs), time.time() - t0))
            if time.time() - t0 > CAT_BUDGET_S:
                print("    分类预算用尽，提前收尾 %s/%s" % (done, len(jobs)))
                break

    groups_out = [{"name": g, "count": sum(len(v) for v in out[g].values()),
                   "cats": [{"name": k, "count": len(v), "songs": v} for k, v in out[g].items()]}
                  for g in out]
    total = sum(x["count"] for x in groups_out)
    print("  分类完成：%s 组 %s 类，共 %s 首（%.0fs）" % (len(groups_out), sum(len(x["cats"]) for x in groups_out), total, time.time() - t0))
    return {"updated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "groups": groups_out, "count": total,
            "note": "分类体系照 QQ 音乐官方（语种/流派/主题/心情/场景）；每类取热门歌单规范化入库"}


# ------------------------------------------------------------------ main
# =========================================================== 真播放：可播直链映射（把"能搜到"变成"真能放"）
STREAM_BUDGET_S = int(os.environ.get("STREAM_BUDGET_S") or "1500")  # 匹配预算（秒/轮，增量续跑）
STREAM_WORKERS = int(os.environ.get("STREAM_WORKERS") or "8")
LYRIC_TOP = int(os.environ.get("LYRIC_TOP") or "320")               # 抓歌词的曲目数（按热度）
COMMENT_TOP = int(os.environ.get("COMMENT_TOP") or "160")           # 抓评论的曲目数
TXT_BUDGET_S = int(os.environ.get("TXT_BUDGET_S") or "900")
MISS_MAX = 4                                                        # 连败 N 轮后不再重试（省时间/防封）


def _skey(title, singer):
    return (A.norm(title) or "")[:40] + "|" + (A.norm(singer) or "")[:30]


def _collect_songs(charts, cats, hero, lib):
    """全站曲目去重成 {skey: {...}}，热度高的优先"""
    acc = {}

    def put(t, s, cov="", dur=0, mid="", seen=0):
        if not t:
            return
        k = _skey(t, s)
        if k == "|":
            return
        e = acc.get(k)
        if e is None:
            acc[k] = {"t": t, "s": s, "cov": cov or "", "dur": dur or 0, "mid": mid or "", "seen": seen or 0}
        else:
            e["seen"] = max(e["seen"], seen or 0)
            if mid and not e["mid"]:
                e["mid"] = mid
            if cov and not e["cov"]:
                e["cov"] = cov
    for s in ((lib or {}).get("songs") or []):
        put(s.get("t"), s.get("s"), s.get("cov"), s.get("dur"),
            ((s.get("ids") or {}).get("qq") or {}).get("mid"), s.get("seen"))
    for l in ((charts or {}).get("lists") or []):
        for s in (l.get("songs") or []):
            put(s.get("title"), s.get("singer"), s.get("cover"), s.get("duration"), s.get("mid"), 1)
    for g in ((cats or {}).get("groups") or []):
        for c in (g.get("cats") or []):
            for s in (c.get("songs") or []):
                put(s.get("t"), s.get("s"), s.get("cov"), s.get("dur"), s.get("mid"), 1)
    for s in ((hero or {}).get("items") or []):
        put(s.get("t"), s.get("s"), s.get("cov"), s.get("dur"), s.get("mid"), 99)
    return acc


def build_streams(charts, cats, hero, lib):
    """给每首歌匹配**实测能播**的长效直链 id（增量：已匹配的不重测）"""
    old = load(os.path.join(DATA, "streams.json"), {})
    omap = dict(old.get("map") or {})
    miss = dict(old.get("miss") or {})
    acc = _collect_songs(charts, cats, hero, lib)
    todo = [k for k in acc if k not in omap and miss.get(k, 0) < MISS_MAX]
    todo.sort(key=lambda k: -acc[k]["seen"])
    print("  曲目去重 %s 首；已可播 %s 首；本轮待匹配 %s 首（冷却中 %s）"
          % (len(acc), sum(1 for k in acc if k in omap), len(todo),
             sum(1 for k in acc if k not in omap and miss.get(k, 0) >= MISS_MAX)))
    t0 = time.time(); done = 0; newmiss = []
    if todo:
        from concurrent.futures import ThreadPoolExecutor as _TPE, as_completed as _ac
        with _TPE(max_workers=STREAM_WORKERS) as ex:
            futs = {ex.submit(A.wy_match, acc[k]["t"], acc[k]["s"]): k for k in todo}
            for fu in _ac(futs):
                if time.time() - t0 > STREAM_BUDGET_S:
                    for f2 in futs:
                        f2.cancel()
                    break
                k = futs[fu]
                try:
                    m = fu.result()
                except Exception:
                    m = None
                if m:
                    omap[k] = {"wy": m["id"], "n": m["name"], "a": m["artist"],
                               "native": 1 if m.get("native") else 0, "dur": m.get("dur") or 0}
                    done += 1
                else:
                    newmiss.append(k)
    for k in newmiss:
        miss[k] = miss.get(k, 0) + 1
    for k in list(omap):
        if k not in acc:
            omap.pop(k)                      # 已不在曲库里的旧键清掉，防膨胀
    miss = {k: v for k, v in miss.items() if k in acc}
    hit = sum(1 for k in acc if k in omap)
    out = {"updated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
           "count": len(acc), "hit": hit, "rate": round(100.0 * hit / max(1, len(acc)), 1),
           "native": sum(1 for k, v in omap.items() if v.get("native")),
           "miss": miss,
           "note": ("key = 归一化(歌名)|归一化(歌手)。App 拼 https://music.163.com/song/media/outer/url?id=<wy>.mp3 "
                    "即为实测可播的长效直链（Range 取回真音频，无需签名）。native=1 表示歌名歌手都吻合。"),
           "map": omap}
    print("  真可播 %s/%s = %s%%（原唱 %s，本轮新增 %s，累计放弃 %s）"
          % (hit, len(acc), out["rate"], out["native"], done,
             sum(1 for k in acc if miss.get(k, 0) >= MISS_MAX)))
    return out


def build_lyrics(acc, streams):
    """抓真歌词（LRC）：优先 QQ 官方词（有 mid），其次网易云词（有 wy id）"""
    old = load(os.path.join(DATA, "lyrics.json"), {})
    lmap = dict(old.get("map") or {})
    smap = (streams or {}).get("map") or {}
    keys = [k for k in acc if k not in lmap]
    keys.sort(key=lambda k: -acc[k]["seen"])
    keys = keys[:LYRIC_TOP]
    t0 = time.time(); ok = 0
    print("  待抓歌词 %s 首（已有 %s 首）" % (len(keys), len(lmap)))

    def one(k):
        e = acc[k]
        if e.get("mid"):
            r = A.qq_lyric(e["mid"])
            if r:
                return k, {"l": r["lyric"], "t": r.get("trans") or "", "src": "qq"}
        sid = (smap.get(k) or {}).get("wy")
        if sid:
            r = A.wy_lyric(sid)
            if r:
                return k, {"l": r["lyric"], "t": r.get("trans") or "", "src": "wy"}
        return k, None

    if keys:
        from concurrent.futures import ThreadPoolExecutor as _TPE, as_completed as _ac
        with _TPE(max_workers=6) as ex:
            futs = [ex.submit(one, k) for k in keys]
            for fu in _ac(futs):
                if time.time() - t0 > TXT_BUDGET_S:
                    break
                try:
                    k, v = fu.result()
                except Exception:
                    continue
                if v:
                    lmap[k] = v
                    ok += 1
    for k in list(lmap):
        if k not in acc:
            lmap.pop(k)
    print("  真歌词 %s 首（本轮新增 %s）" % (len(lmap), ok))
    return {"updated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "count": len(lmap), "note": "按 key 索引，l=LRC 原文、t=翻译、src=来源",
            "map": lmap}


def build_comments(acc, streams):
    """抓真评论（网易云热评）"""
    old = load(os.path.join(DATA, "comments.json"), {})
    cmap = dict(old.get("map") or {})
    smap = (streams or {}).get("map") or {}
    keys = [k for k in acc if k not in cmap and (smap.get(k) or {}).get("wy")]
    keys.sort(key=lambda k: -acc[k]["seen"])
    keys = keys[:COMMENT_TOP]
    t0 = time.time(); ok = 0
    print("  待抓评论 %s 首（已有 %s 首）" % (len(keys), len(cmap)))
    if keys:
        from concurrent.futures import ThreadPoolExecutor as _TPE, as_completed as _ac
        with _TPE(max_workers=6) as ex:
            futs = {ex.submit(A.wy_comments, (smap.get(k) or {}).get("wy"), 20): k for k in keys}
            for fu in _ac(futs):
                if time.time() - t0 > TXT_BUDGET_S:
                    break
                k = futs[fu]
                try:
                    v = fu.result()
                except Exception:
                    v = None
                if v:
                    cmap[k] = v
                    ok += 1
    for k in list(cmap):
        if k not in acc:
            cmap.pop(k)
    print("  真评论 %s 首（本轮新增 %s）" % (len(cmap), ok))
    return {"updated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "count": len(cmap), "note": "按 key 索引；hot=热评 new=最新，l=点赞数",
            "map": cmap}


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

    print("  —— 主视觉（高清海报轮播）——")
    try:
        hero = build_hero(charts)
    except Exception:
        print(traceback.format_exc())
        hero = {}

    print("  —— 歌库分类 ——")
    try:
        cats = build_categories()
    except Exception:
        print(traceback.format_exc())
        cats = {}

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
    if hero:
        save(os.path.join(DATA, "hero.json"), hero, indent=None)
    if cats:
        save(os.path.join(DATA, "categories.json"), cats, indent=None)

    print("  —— 真播放链路（可播直链 / 歌词 / 评论）——")
    streams, lyrics, comments = {}, {}, {}
    try:
        streams = build_streams(charts, cats, hero, lib)
    except Exception:
        print(traceback.format_exc())
    if streams.get("map"):
        acc = _collect_songs(charts, cats, hero, lib)
        try:
            lyrics = build_lyrics(acc, streams)
        except Exception:
            print(traceback.format_exc())
        try:
            comments = build_comments(acc, streams)
        except Exception:
            print(traceback.format_exc())
    if streams:
        save(os.path.join(DATA, "streams.json"), streams, indent=None)
    if lyrics:
        save(os.path.join(DATA, "lyrics.json"), lyrics, indent=None)
    if comments:
        save(os.path.join(DATA, "comments.json"), comments, indent=None)

    summary = ", ".join("%s(%s/%s)" % (h["id"], h["role"], h["score"]) for h in active)
    print("\n试源顺序: " + " → ".join(order) + "   [高保真 " + summary + "]")
    print("自有歌库: %s 首（榜单扫描 %s，新入库 %s）；歌手头像 %s 个；海报主色 %s 个（本轮新增 %s）；补给日志 %s"
          % (lib["count"], lib["chart_scanned"], lib["chart_new"], arts["count"],
             lib.get("hue_total"), lib.get("hue_new"), replenish))
    if mv:
        print("MV/演唱会: %s 条（%s 个合集，实测取流通过 %s 条 %s）"
              % (mv["count"], len(mv["collections"]), mv["verified"], mv["verified_quality"]))
    if hero:
        print("主视觉: %s 张 800×800 高清海报（主色 %s 张）" % (hero["count"], hero["hue_ok"]))
    if streams:
        print("真播放: %s/%s 首可播 = %s%%（原唱 %s）；真歌词 %s 首；真评论 %s 首"
              % (streams["hit"], streams["count"], streams["rate"], streams["native"],
                 lyrics.get("count", 0), comments.get("count", 0)))
    print("已写 data/pool.json / data/charts.json / data/library.json / data/artists.json"
          + (" / data/mv.json" if mv else "") + (" / data/hero.json" if hero else "")
          + (" / data/categories.json" if cats else "")
          + (" / data/streams.json / data/lyrics.json / data/comments.json" if streams else ""))


if __name__ == "__main__":
    main()
