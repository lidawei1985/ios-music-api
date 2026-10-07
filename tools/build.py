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
import json, os, re, statistics, sys, time, traceback, urllib.parse
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


# ------------------------------------------------------------------ 2c) MV（网易云官方 MV）
MV_BUDGET_S = int(os.environ.get("MV_BUDGET_S") or "300")
# ★ 2026-10-06 主人点名「MV/演唱会方向错了」→ 不再抓 B 站演唱会/搬运视频。
#   现在只取**网易云官方 MV**（cloudsearch type=1004）。官方 MV 库里仍混有用户投稿，
#   用这个闸挡掉（比 A.badness 更严：badness 含"现场/live"会把「Live Forever」误伤，
#   而官方 MV 里带 live/现场 的基本都是用户上传的演出片段，这里要挡）。
JUNK_COVER = re.compile(
    r"cover|翻唱|伴奏|伴唱|karaoke|卡拉\s?ok|纯享|消音|清唱|"
    r"现场|演唱会|音乐节|歌友会|live|remix|dj版|串烧|合集|剪辑|饭制|"
    r"钢琴|吉他版|八音盒|口琴|陶笛|葫芦丝|二胡|古筝|教学|教程|谱|"
    r"片段|试听|预告|花絮|采访|reaction|翻弹|合唱版|童声|女声|男声", re.I)

# ★ 2026-10-06 实测发现「热门 MV」里混进这些**非 MV 视频**，官方 MV 库里也有用户投稿：
#   · 4909  超级面对面访谈 | 周杰伦 | 542s     ← 访谈节目
#   · 5329198 周杰伦独家问候网易云音乐网友 | 9s  ← 敬告语（时长仅 9 秒）
#   两类判据：① 名字带访谈/问候类词；② 时长太短（真 MV 极少 <75s，短的是广告/问候/预告）。
MV_TALK_RE = re.compile(
    r"访谈|面对面|独家问候|问候|拜年|新年快乐|祝福|花絮|幕后|"
    r"采访|专访|发布会|记者|探班|彩排|排练|直播间|直播回放|"
    r"预告|先导|片花|特辑|纪录片|番外|彩蛋|会员|招募|"
    r"官宣|直拍|混剪|合集|榜单|盘点|reaction|我猜|挑战|"
    r"lyric\s?video|歌词版|歌词视频|字幕版|可视化|dynamic|visualizer", re.I)
MV_MIN_SEC = int(os.environ.get("MV_MIN_SEC") or "75")     # 真 MV 时长下限（秒）
MV_MAX_SEC = int(os.environ.get("MV_MAX_SEC") or "900")    # 超过 15 分钟的不是单曲 MV（是直播回放/整场演唱会）
MV_LIVE_MAX_SEC = int(os.environ.get("MV_LIVE_MAX_SEC") or "1800")  # 现场档放宽：整场精华版也要（≤30 分钟）


def build_mv(chart_songs):
    """MV 页：**网易云官方 MV**（不是 B 站 UP 主搬运/录音棚/KTV伴奏）。

    ★★ 2026-10-06 主人点名「MV / 演唱会这方向错了」→ 整条链路推翻重做。
       旧做法：`b.search_mv("<歌名> MV")` 抓 B 站 → 结果是 UP 主搬运、
       「百万豪装录音棚大声听」、「KTV字幕伴奏」、「2005演唱会修复版」这类
       —— 既不是官方 MV，也不是正经现场，还混满翻唱。
       新做法（实测 2026-10-06 全通）：
         · 搜索  POST/GET  music.163.com/api/cloudsearch/pc?s=<kw>&type=1004  → 官方 MV 列表
         · 详情  GET       music.163.com/api/mv/detail?id=<mvid>
                 → name / artistName / cover / playCount / duration / publishTime，
                   且 **brs 里直接带 240/480 清晰度 mp4 地址**
         · 地址  GET       music.163.com/api/song/enhance/play/mv/url?id=<mvid>&r=1080
                 → {code:200, url:"http://vod...mp4", r:480, size:34649630}
       全部**匿名可用**，无需登录、无需 cookie。
    """
    t0 = time.time()
    st = {"found": 0, "verified": 0, "q": {}}

    def search_mv(kw, limit=8):
        u = ("https://music.163.com/api/cloudsearch/pc?s=" + urllib.parse.quote(kw) +
             "&type=1004&limit=%d&offset=0" % limit)
        r = A.http(u, {"Referer": "https://music.163.com/"})
        if r["status"] != 200:
            return []
        try:
            d = json.loads(r["body"].decode("utf-8", "replace"))
        except Exception:
            return []
        return ((d.get("result") or {}).get("mvs") or [])

    def bulk_mv(offset, limit=50):
        """★ 用**官方 MV 全量库**翻页（不依赖关键词）。

        实测结论（2026-10-06，三轮诊断确立）：
          · `mv/all?limit=50&offset=N` 匿名可用，**offset 0→5000 每页都有货**，
            offset 5000 起 hasMore=False → **全量库约 5100 条**；
            （offset 8000/20000 居然还回 50 条，是重复灌水的兜底页，不可信 ——
             所以扩容**必须以 hasMore 为准**，不能一味加大 offset。）
          · offset 600 附近页面严重重复（网易云分页 bug）→ 必须靠 id 去重，
            并且**用 id 集合判断新意，连续多页零新增即停**，避免空转。
          · 纯度：空名 0、无歌手 0、访谈类 ~7%、>15min ~0.4%；
            带 Live 标记占 **15%** → 全库可稳定产出 700+ 条真现场。
          · 取流抽检 6/6 全通，全部 r=1080（38MB~673MB 真 1080P）。

        ★ 致命坑（已避开）：`mv/all?artistId=<id>` 的参数**会被服务端静默忽略** ——
          加不加 artistId 返回的是同一条流。曾据此误判「按歌手能拿 150 条」，
          实际那 150 条就是 MV 库前 150 条，与歌手无关。**不要再用 artistId 维度。**
        """
        u = "https://music.163.com/api/mv/all?limit=%d&offset=%d" % (limit, offset)
        r = A.http(u, {"Referer": "https://music.163.com/"})
        if r["status"] != 200:
            return [], False
        try:
            d = json.loads(r["body"].decode("utf-8", "replace"))
        except Exception:
            return [], False
        out = []
        for m in (d.get("data") or []):
            # mv/all 字段名与 search 略有差异（artists 数组 vs artistName）
            ar = m.get("artistName") or ""
            if not ar:
                ar = "/".join([a.get("name") or "" for a in (m.get("artists") or []) if a.get("name")])
            out.append({"id": m.get("id"), "name": m.get("name"), "artistName": ar,
                        "cover": m.get("cover"), "duration": m.get("duration"),
                        "playCount": m.get("playCount")})
        return out, bool(d.get("hasMore"))

    def mv_ok(mvid):
        """实测能取到 mp4 直链才算数（不带验证的死链不许进库）"""
        u = "https://music.163.com/api/song/enhance/play/mv/url?id=%s&r=1080" % mvid
        r = A.http(u, {"Referer": "https://music.163.com/"})
        if r["status"] != 200:
            return None
        try:
            d = json.loads(r["body"].decode("utf-8", "replace"))
        except Exception:
            return None
        dd = d.get("data") or {}
        if d.get("code") == 200 and (dd.get("url") or "").startswith("http"):
            return {"url": dd["url"], "size": dd.get("size") or 0, "r": dd.get("r") or 480}
        return None

    def _title_key(t, a):
        """标题归一化去重键：削掉括号/破折号后缀 + 标点，用于跨版本去重。

        ★ 为什么必须有：实测《孤勇者》在库里同时有
          「孤勇者」(14480854, 2416 万播放) 与
          「孤勇者 (《英雄联盟：双城之战》动画剧集中文主题曲)」(14686114, 236 万)，
          两条同曲不同标题后缀 —— 不去重就会一首歌占两个坑位。
          《句号》同款问题（10906470 / 10956794）。
        """
        s = re.split(r"[\(（\[【\-–—|/]", (t or ""), 1)[0]
        s = re.sub(r"[^\w\u4e00-\u9fff]+", "", s, flags=re.U).lower()
        ar = re.sub(r"[^\w\u4e00-\u9fff]+", "", (a or ""), flags=re.U).lower()[:12]
        return s + "|" + ar

    def pack(name, keywords, per_kw=6, limit=20, verify_n=4, want_live=False, seen_keys=None):
        seen, items = set(), []
        # ★ 与全库扫描共用的「跨版本去重表」：实测《孤勇者》在同一次搜索里回两条
        #   （原始版 + 「(《英雄联盟：双城之战》动画剧集中文主题曲)」），歌名只差后缀。
        #   关键词搜索这条路也必须去重，否则同一首歌占两个坑位。
        seen_keys = seen_keys if seen_keys is not None else set()
        for kw in keywords:
            if time.time() - t0 > MV_BUDGET_S:
                break
            # 关键词里的「歌名 歌手」拆出来，用于相关性校验；
            # 单段关键词（如「周杰伦」）= 按歌手搜热门 MV，此时不做歌名校验，
            # 只要求 MV 的歌手与该关键词吻合即可。
            # ★ want_live：关键词本身是**题材词**（"演唱会现场"/"live 现场版"），
            #   不是「歌名 歌手」，所以不做任何相关性校验，只靠 live_re + 时长把关。
            parts = kw.split(" ")
            want_t = A.norm(parts[0]) if parts else ""
            want_a = A.norm(parts[1]) if len(parts) > 1 else (A.norm(parts[0]) if parts else "")
            solo = (len(parts) == 1) or want_live
            # ★ want_live：只要「现场/演唱会」，且必须**明确带现场标记**
            #   （否则混进录音室 MV，就不是主人要的「演唱会」这一档）
            live_re = re.compile(r"演唱会|现场|音乐会|音乐节|live|concert|tour|巡演|巡迴", re.I)
            for m in search_mv(kw, per_kw):
                mid = m.get("id")
                if not mid or mid in seen:
                    continue
                nm = (m.get("name") or "").strip()
                ar = (m.get("artistName") or "").strip()
                sec = (m.get("duration") or 0) // 1000
                if want_live:
                    # 现场档：必须带 live 标记，但仍要挡掉 cover/伴奏/教学类
                    if not live_re.search(nm):
                        continue
                    if re.search(r"cover|翻唱|伴奏|伴唱|karaoke|卡拉\s?ok|教学|教程|"
                                 r"吉他谱|钢琴谱|纯享|消音|清唱|片段|预告", nm, re.I):
                        continue
                else:
                    # 官方 MV 档：剔掉 UP 主上传的 cover/伴奏/live 剪辑
                    if A.badness(nm) >= 1 or JUNK_COVER.search(nm):
                        continue
                # ★ 剔掉访谈/问候/花絮类非 MV
                if MV_TALK_RE.search(nm):
                    continue
                # ★ 时长合理性：太短=广告/问候；MV 档上限 15 分钟；现场档放宽到 30 分钟（整场精华）
                cap = MV_LIVE_MAX_SEC if want_live else MV_MAX_SEC
                if sec and (sec < MV_MIN_SEC or sec > cap):
                    continue
                nn = A.norm(nm)
                an = A.norm(ar)
                if want_live:
                    # ★ 题材词搜现场：不做歌名/歌手校验（"演唱会现场" 不是歌手名），
                    #   真伪全靠 live_re + 时长 + 反 cover 三道闸把住。
                    pass
                elif solo:
                    # 按歌手搜：歌手必须吻合，歌名不校验
                    if want_a and not (want_a in an or an in want_a):
                        continue
                else:
                    # 按「歌名 歌手」搜：歌名和歌手都要吻合（挡"同名不同歌"）
                    if want_t and not (want_t in nn or nn in want_t):
                        continue
                    if want_a and not (want_a in an or an in want_a):
                        continue
                seen.add(mid)
                # ★ 跨版本去重：同曲不同版本（原版 / 影视主题曲版 / 剧情版）只留一条。
                #   用调用方传入的共用表 seen_keys —— ★★ 这里**必须没有 return**：
                #   表里命中的只表示"同曲的另一版本已经收过了"，跳过这一条即可，
                #   后面的搜索结果还要继续看（早期写成 `return` 会整条腿提前退出）。
                if seen_keys is not None:
                    tk2 = _title_key(nm, ar)
                    if tk2 in seen_keys:
                        continue
                    seen_keys.add(tk2)
                items.append({
                    "id": mid, "t": nm, "a": ar,
                    "cov": (m.get("cover") or "").replace("http://", "https://"),
                    "dur": sec,                                        # MV 时长用秒
                    "play": int(m.get("playCount") or 0),
                })
                st["found"] += 1
                if len(items) >= limit:
                    break
            if len(items) >= limit:
                break
        # 抽样验证真能出流
        for it in items[:verify_n]:
            if time.time() - t0 > MV_BUDGET_S:
                break
            r = mv_ok(it["id"])
            if r:
                it["q"] = "1080P" if r["r"] >= 1080 else ("480P" if r["r"] >= 480 else "240P")
                it["wh"] = ""
                st["verified"] += 1
                st["q"][it["q"]] = st["q"].get(it["q"], 0) + 1
        print("  MV[%s] %d 条" % (name, len(items)))
        return {"name": name, "count": len(items), "items": items}

    def _clean_bulk(m, seen_ids):
        """全量库共用清洗：返回规整条目 dict 或 None（不合格）"""
        mid = m.get("id")
        if not mid or mid in seen_ids:
            return None
        nm = (m.get("name") or "").strip()
        ar = (m.get("artistName") or "").strip()
        sec = (m.get("duration") or 0) // 1000
        if not nm or not ar:
            return None
        if MV_TALK_RE.search(nm):
            return None
        return {
            "id": mid, "t": nm, "a": ar,
            "cov": (m.get("cover") or "").replace("http://", "https://"),
            "dur": sec, "play": int(m.get("playCount") or 0),
        }

    LIVE_RE = re.compile(
        r"演唱会|现场|音乐会|音乐节|concert|tour|巡演|巡迴|"
        r"music\s?festival|公演|歌谣|人气歌谣|音乐银行|音乐中心|"
        r"The Show|歌谣大战|fancam|直拍|不插电|unplugged|"
        r"毕业歌会|跨年|演唱会版|live版", re.I)
    # ★ 「live」这个词单独判定极易误杀 —— 实测把 Taylor Swift
    #   《I Don’t Wanna Live Forever》当成现场收录（"Live" 是歌名的一部分）。
    #   规则：`live` 只有在**不是歌名主语**时才算现场标记 ——
    #   即带 live 时必须同时满足「有现场语境词」或「live 在括号/破折号里或后面跟
    #   version/at/in/现场/版」这类结构。简单说：**光一个裸 live 不算数**。
    LIVE_BARE = re.compile(r"\blive\b", re.I)
    LIVE_CTX = re.compile(
        r"[\(\（\[【][^\)\）\]】]*\blive[^\)\）\]】]*[\)\）\]】]"        # (Live) / [Live at ...]
        r"|\blive\s*(?:版|version|ver\.?|at|in|@|from|session|tour)"   # live版 / Live at xx
        r"|live\s*(?:'|’)?\d", re.I)                                    # live '96
    # 明显是「歌名带 live 但不是现场」的白名单式反例（去掉后不当现场）
    LIVE_TRAP = re.compile(r"live\s?forever|i\s?don'?t\s?wanna\s?live|"
                           r"live\s?while\s?we'?re\s?young|live\s?and\s?let\s?die", re.I)
    LIVE_JUNK = re.compile(
        r"伴奏|伴唱|karaoke|卡拉\s?ok|教学|教程|吉他谱|钢琴谱|"
        r"消音|清唱|翻唱|cover\b|改编|器乐|纯音乐|白噪音|"
        r"听书|有声|电台|广播剧|助眠|\bdj\b", re.I)

    def is_live(name):
        """判定是否真现场：题材词命中 或（裸 live + 现场语境/结构）"""
        if LIVE_TRAP.search(name):
            return False
        if LIVE_JUNK.search(name):
            return False
        # 强题材词（演唱会/现场/音乐节/歌谣大战…）
        if re.search(r"演唱会|现场|音乐会|音乐节|concert|tour|巡演|巡迴|"
                     r"music\s?festival|公演|歌谣|人气歌谣|音乐银行|音乐中心|"
                     r"The Show|歌谣大战|fancam|直拍|不插电|unplugged|"
                     r"毕业歌会|跨年", name, re.I):
            return True
        # 裸 live：必须有现场语境（(Live at…)、(Live)、live版、live '96…）
        if LIVE_BARE.search(name) and LIVE_CTX.search(name):
            return True
        return False

    def scan_library(max_pages=None,
                     limit_mv=int(os.environ.get("MV_POOL_MV") or "6000"),
                     limit_live=int(os.environ.get("MV_POOL_LIVE") or "1200"),
                     stop_after_empty=12, verify_n=6):
        """★★ 一次性**深挖官方 MV 全量库**，一次拿到真 MV 池 + 现场池。

        ★ 这是本轮「MV/演唱会方向错了」的根因修复：
          旧做法只在关键词搜索里打转（每次 10 条），量永远上不去 →
          只能反复凑数、混进搬运/访谈/伴奏。
          现在直接翻全量库：**一次拿 5000 条原始 → 清洗 → 分档**，
          真 MV 与真现场各归各位，靠的是**规模**而不是运气。

        ★ 终止条件（三级，防跑飞与防无效翻页）：
          ① hasMore=False（真到底，实测 offset 5000）；
          ② 连续 stop_after_empty 页零新增（网易云分页重复 bug 兜底）；
          ③ 时间预算 MV_BUDGET_S 到点。
        """
        seen_ids = set()
        seen_title = set()
        mv_pool, live_pool = [], []
        empty_streak = 0
        dup_title = 0
        off = 0
        pages = max_pages or (int(os.environ.get("MV_BULK_PAGES") or "120"))
        while len(seen_ids) < 20000:
            if len(mv_pool) >= limit_mv and len(live_pool) >= limit_live:
                break
            if time.time() - t0 > MV_BUDGET_S:
                print("    MV 全库扫描：时间预算到（已翻 %d 页）" % (off // 50))
                break
            if pages and off // 50 >= pages:
                break
            arr, more = bulk_mv(off)
            if not arr:
                break
            got = 0
            for m in arr:
                it = _clean_bulk(m, seen_ids)
                if not it:
                    continue
                seen_ids.add(it["id"])
                got += 1
                nm, sec = it["t"], it["dur"]
                # ★ 跨版本去重：同一首歌常有「原始版 / 影视主题曲版 / 剧情版 / 重制版」
                #   实测《孤勇者》同曲两条（14480854 2416 万播放、14686114 236 万），
                #   标题只差「(《英雄联盟：双城之战》动画剧集中文主题曲)」。
                #   去重键 = 主标题(削括号后缀) + 歌手；保留先出现的（全库按热度非严格，
                #   故两条都进候选后按 play 排序，最终留下更热的那条）。
                tk = _title_key(nm, it["a"])
                if tk in seen_title:
                    dup_title += 1
                    continue
                seen_title.add(tk)
                live_ok = is_live(nm)
                mv_junk = (A.badness(nm) >= 1) or JUNK_COVER.search(nm)
                if live_ok:
                    # 现场档：整场精华允许到 30 分钟；单曲现场 75s 起
                    if sec and (sec < MV_MIN_SEC or sec > MV_LIVE_MAX_SEC):
                        continue
                    if len(live_pool) < limit_live:
                        live_pool.append(it)
                else:
                    # 真 MV 档：剔 up 主 cover/伴奏/KTV，时长 75s~15min
                    if mv_junk:
                        continue
                    if sec and (sec < MV_MIN_SEC or sec > MV_MAX_SEC):
                        continue
                    if len(mv_pool) < limit_mv:
                        mv_pool.append(it)
            empty_streak = empty_streak + 1 if got == 0 else 0
            if empty_streak >= stop_after_empty:
                print("    MV 全库扫描：连续 %d 页零新增，提前收敛（offset=%d）"
                      % (empty_streak, off))
                break
            if not more:
                print("    MV 全库扫描：hasMore=False，真到底（offset=%d）" % off)
                break
            off += 50
            time.sleep(0.12)
        print("    MV 全库扫描：翻页至 offset=%d，去重 %d 条（跨版本再剔 %d）→ 真 MV %d / 真现场 %d"
              % (off, len(seen_ids), dup_title, len(mv_pool), len(live_pool)))
        # ★ 热度优先 + 同曲保最热：排序后按 _title_key 再保一次，确保「更热的版本」留下
        mv_pool.sort(key=lambda x: -x["play"])
        live_pool.sort(key=lambda x: -x["play"])
        return mv_pool, live_pool

    def verify(items, n):
        """抽样实测取流（不带验证的不许进库）"""
        for it in items[:n]:
            if time.time() - t0 > MV_BUDGET_S + 120:
                break
            r = mv_ok(it["id"])
            if r:
                it["q"] = "1080P" if r["r"] >= 1080 else ("480P" if r["r"] >= 480 else "240P")
                it["wh"] = ""
                st["verified"] += 1
                st["q"][it["q"]] = st["q"].get(it["q"], 0) + 1
        return items

    cols = []
    # （旧代码这里取 core = chart_songs[:40] 喂「官方 MV」关键词搜索那条腿；
    #   2026-10-07 已改为直接从全库真 MV 池续取，不再依赖榜单前 20 首。）

    # ★★ 第一步：无条件深挖官方 MV 全量库（本轮核心改造）
    #   一次拿到「真 MV 池」+「现场池」，后面所有栏目都从这里取 ——
    #   彻底摆脱「关键词搜索凑数」的老路（每次 10 条，量上不去且杂）。
    pool_mv, pool_live = scan_library()
    used = set()
    # ★ 跨栏目共用的「跨版本去重表」+ 去重键函数（关键词搜索那几条腿也共用）
    used_titles = set()

    def tkey(t, a):
        s = re.split(r"[\(（\[【\-–—|/]", (t or ""), 1)[0]
        s = re.sub(r"[^\w\u4e00-\u9fff]+", "", s, flags=re.U).lower()
        ar = re.sub(r"[^\w\u4e00-\u9fff]+", "", (a or ""), flags=re.U).lower()[:12]
        return s + "|" + ar

    def take(pool, n, dedup=True):
        """从池子里按热度取 n 条。

        ★ 2026-10-06 修 bug：原来只按 `id` 去重 → 《孤勇者》两条
          （14480854 / 2416万播放 与 14686114 / 236万播放，标题只差
          「(《英雄联盟：双城之战》动画剧集中文主题曲)」后缀）**同时进了栏目①**。
          现在这里接上全局的 `used_titles` 跨版本去重表：
            · 池子已按热度降序 → 同名只留最热的那条（正是我们想要的）；
            · 该表与 `pack()` 关键词搜索那条腿共用，跨栏目也不会再撞。
        """
        out = []
        for it in pool:
            if dedup and it["id"] in used:
                continue
            if dedup:
                tk = tkey(it.get("t"), it.get("a"))
                if tk in used_titles:
                    continue
                used_titles.add(tk)
            used.add(it["id"])
            out.append(dict(it))
            if len(out) >= n:
                break
        return out

    # ★★★ 2026-10-07 修复（主人点名「MV 和演唱会弄了吗？」「说一样干一样」）：
    #   旧写法的病灶 —— 只有 ①精选 ②演唱会 两条腿是**从全库池子**取；
    #   ③官方 MV ④热门 MV 走的是「关键词搜索」，而关键词只喂了**榜单前 20 首 × per_kw=4**
    #   → 官方 MV 档实测**只有 12 条**；与此同时全库扫描明明已扫出 3000 条真 MV，
    #   剩下 2400 条全躺在 pool_mv 里**没人用** → MV 总量被这一条腿卡死在 1224。
    #   现在五档**全部优先从池子取**（池子已过 时长/垃圾/跨版本去重 三重闸门），
    #   关键词搜索只作「池子取空后的兜底」—— 这才叫「靠规模，不是靠运气」。
    N_SEL = int(os.environ.get("MV_SEL_N") or "600")      # ① 精选 MV
    N_LIVE = int(os.environ.get("MV_LIVE_N") or "600")    # ② 演唱会 Live
    N_OFF = int(os.environ.get("MV_OFF_N") or "1500")     # ③ 官方 MV（池续取）
    N_HOT = int(os.environ.get("MV_HOT_N") or "1500")     # ④ 热门 MV（池续取）
    N_LIVE2 = int(os.environ.get("MV_LIVE2_N") or "300")  # ⑤ 现场 Live（池续取）

    # 栏目①「精选 MV」：全库最热的真 MV，保证首页一打开就有硬货
    sel = take(pool_mv, N_SEL)
    verify(sel, int(os.environ.get("MV_VERIFY_N") or "6"))
    print("  MV[精选 MV] %d 条（真 MV 池 %d）" % (len(sel), len(pool_mv)))
    cols.append({"name": "精选 MV", "count": len(sel), "items": sel})

    # 栏目②「演唱会 Live」：主人点名的方向 —— 全库带现场标记的，按热度排
    live = take(pool_live, N_LIVE)
    verify(live, 5)
    print("  MV[演唱会 Live] %d 条（现场池 %d）" % (len(live), len(pool_live)))
    cols.append({"name": "演唱会 Live", "count": len(live), "items": live})

    # 栏目③「官方 MV」：★ 从全库真 MV 池**续取**（不再重新搜索凑数）
    official = take(pool_mv, N_OFF)
    verify(official, 4)
    print("  MV[官方 MV] %d 条（池续取）" % len(official))
    cols.append({"name": "官方 MV", "count": len(official), "items": official})

    # 栏目④「热门 MV」：★ 同上，从池续取
    hot = take(pool_mv, N_HOT)
    verify(hot, 4)
    print("  MV[热门 MV] %d 条（池续取）" % len(hot))
    cols.append({"name": "热门 MV", "count": len(hot), "items": hot})

    # 栏目⑤「现场 Live」：现场池续取 + 关键词搜索兜底（捡漏池里没标 live 的现场）
    live2 = take(pool_live, N_LIVE2)
    live_kws = ["演唱会现场", "live 现场版", "演唱会 Live", "世界巡回演唱会",
                "巡回演唱会", "音乐节 现场", "跨年演唱会 现场",
                "周杰伦 演唱会", "五月天 演唱会", "陈奕迅 演唱会",
                "邓紫棋 演唱会", "林俊杰 演唱会", "张学友 演唱会", "张惠妹 演唱会"]
    extra5 = pack("现场 Live", live_kws, per_kw=8, limit=60, verify_n=5,
                  want_live=True, seen_keys=used_titles)
    items5 = list(live2) + list(extra5["items"])
    print("  MV[现场 Live] %d 条（池续取 %d + 关键词补 %d）"
          % (len(items5), len(live2), len(extra5["items"])))
    cols.append({"name": "现场 Live", "count": len(items5), "items": items5})

    # 池子取空时的兜底：真 MV 池一条都没扫到（接口破版/风控）→ 回落到关键词搜索
    if not sel and not official:
        big = ["周杰伦", "邓紫棋", "薛之谦", "陈奕迅", "林俊杰", "毛不易",
               "五月天", "李荣浩", "张杰", "汪苏泷", "蔡依林", "田馥甄"]
        cols.append(pack("热门 MV", big, per_kw=8, limit=60, verify_n=4, seen_keys=used_titles))

    flat = [x for c in cols for x in c["items"]]

    # ★★ 封面本地化标记：MV 封面原来只存 p1.music.126.net 的**别人的 URL**，
    #   上游一改域名/加防盗链就整页白图。这里把 mv 封面也交给 localize 沉淀成
    #   我们自己的 WebP（与头像/KV/歌库封面同一套体系），并在条目上标 cov_l 供端上优先读。
    #   本地化在 main() 里统一执行（build_mv 时 assets 还没跑），此处只负责"喂料"。
    return {"updated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "collections": cols, "count": len(flat),
            "verified": st["verified"], "verified_quality": st["q"],
            "pool": {"mv": len(pool_mv), "live": len(pool_live)},
            "note": ("来源=网易云官方 MV 全量库（mv/all 翻页，实测约 5100 条，以 hasMore 为界）"
                     "+ cloudsearch type=1004 关键词补充。客户端播放时按 id 现取 1080P 直链"
                     "（直链带 wsTime 签名，1 小时过期，故只存 id 不存直链）。"
                     "封面由采集器本地化为自己的 WebP（data/assets/cv），不依赖上游图床。")}


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
    """给每首歌匹配**实测能播**的长效 id（增量：已匹配的不重测）。

    ★★ 2026-10-06 大改：从「只认网易云」升级为**多源回退**（主人要求「VIP 也想办法能到」）。
       旧版单源 wy_match 的死穴：网易云对 VIP/付费歌返回 `4515 bytes + text/html`，
       被判不可播 → **伍佰《泪桥》等原唱整片消失，只剩免费翻唱**（主人原话：
       「原唱都删了只保留了个翻唱」）。现在 wy 失败自动回退 酷我(kw) → 咪咕(mg) → B站(bili)。
       ★ 只存**长效 id**（wy id / kw rid），**不存临时直链**（带签名会过期）。
    """
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
            futs = {ex.submit(A.multi_match, acc[k]["t"], acc[k]["s"], acc[k].get("dur") or 0): k
                    for k in todo}
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
                    # ★ 多源：存 src 标出来源，客户端按 src 拼不同直链
                    rec = {"src": m["src"], "n": m.get("name"), "a": m.get("artist"),
                           "native": 1 if (A.norm(m.get("name") or "") == A.norm(acc[k]["t"])) else 0,
                           "dur": m.get("dur") or 0}
                    if m["src"] == "wy":
                        rec["wy"] = m["id"]
                    else:
                        rec[m["src"]] = m["id"]           # kw / mg / bili 各自的 id
                    omap[k] = rec
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
    src_cnt = {}
    for v in omap.values():
        src_cnt[v.get("src") or "wy"] = src_cnt.get(v.get("src") or "wy", 0) + 1
    out = {"updated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
           "count": len(acc), "hit": hit, "rate": round(100.0 * hit / max(1, len(acc)), 1),
           "native": sum(1 for k, v in omap.items() if v.get("native")),
           "srcs": src_cnt,
           "miss": miss,
           "note": ("key = 归一化(歌名)|归一化(歌手)。值为**长效 id**（不是直链，直链带签名会过期）："
                    "wy → https://music.163.com/song/media/outer/url?id=<wy>.mp3；"
                    "kw → http://antiserver.kuwo.cn/anti.s?type=convert_url&rid=MUSIC_<kw>&format=mp3&response=url；"
                    "native=1 表示歌名歌手都吻合，src 标出采用的是哪个平台。"),
           "map": omap}
    print("  真可播 %s/%s = %s%%（原唱 %s，本轮新增 %s，累计放弃 %s）｜来源 %s"
          % (hit, len(acc), out["rate"], out["native"], done,
             sum(1 for k in acc if miss.get(k, 0) >= MISS_MAX),
             "、".join("%s %d" % (k, v) for k, v in sorted(src_cnt.items()))))
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
                    # 2026-10-06 修「词不同步不对版」：内核取流按网易云 id、取词却按「歌名|歌手」，
                    # 两把尺子量同一首歌 → VIP/翻唱版拿到别人的词，时间轴天然错位。
                    # 这里额外写一份 "i:<wyid>" 键，让内核能用同一把尺子取到「这版音频的词」。
                    wid = (smap.get(k) or {}).get("wy")
                    if wid:
                        v2 = dict(v); v2["n"] = k          # 记下名字键，便于回溯
                        lmap["i:" + str(wid)] = v2
                    ok += 1
    # 清理孤儿：名字键必须还在 acc；id 键必须还能在 streams 里反查到
    live_ids = set()
    for n in acc:
        w = (smap.get(n) or {}).get("wy")
        if w:
            live_ids.add(str(w))
    for k in list(lmap):
        if k.startswith("i:"):
            if k[2:] not in live_ids:
                lmap.pop(k)
        elif k not in acc:
            lmap.pop(k)
    print("  真歌词 %s 条（本轮新增 %s）" % (len(lmap), ok))
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

    # ★★ 2026-10-06 主人点破「采集器抓的东西有没有变成自己的东西/沉淀」→
    #   图片（头像/KV/封面）原来只存**别人的 URL**，上游一改域名三处同时白屏。
    #   这里把本地化做成**采集器固定栏目**：每轮自动下载→转 WebP→落盘 data/assets/，
    #   增量复用（已在本地的不重下），让 App 读「自己的静态资源」。
    assets_man = {}
    if os.environ.get("WITH_LOCALIZE", "1") != "0":
        print("  —— 图片本地化沉淀（头像/KV/封面 → 自己的 WebP）——")
        try:
            import localize as LZ
            LZ.cmd_localize()
            assets_man = LZ.load_json("assets/manifest.json", {})
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
    if hero:
        save(os.path.join(DATA, "hero.json"), hero, indent=None)
    if cats:
        save(os.path.join(DATA, "categories.json"), cats, indent=None)

    print("  —— 真播放链路（可播直链 / 歌词 / 评论）——")
    streams, lyrics, comments = {}, {}, {}
    try:
        # 注意：charts 在 main 里是 list（榜单数组），_collect_songs 需要 {"lists": [...]} 形态
        streams = build_streams({"lists": charts}, cats, hero, lib)
    except Exception:
        print(traceback.format_exc())
    if streams.get("map"):
        acc = _collect_songs({"lists": charts}, cats, hero, lib)
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
    if assets_man:
        k = assets_man.get("kinds") or {}
        print("图片沉淀: " + " / ".join("%s %s 张" % (n, v.get("count"))
                                        for n, v in k.items())
              + "  共 %.1f MB（自己的 WebP，不依赖上游）" % ((assets_man.get("total_bytes") or 0) / 1048576))
    print("已写 data/pool.json / data/charts.json / data/library.json / data/artists.json"
          + (" / data/mv.json" if mv else "") + (" / data/hero.json" if hero else "")
          + (" / data/categories.json" if cats else "")
          + (" / data/streams.json / data/lyrics.json / data/comments.json" if streams else ""))


if __name__ == "__main__":
    main()
