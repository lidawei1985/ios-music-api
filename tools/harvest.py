#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
大伟哥 MUSIC · 曲库批量采集（主干 = 网易云歌单）

接口实测结论（决定了整个流程的形状）：
  · 歌单列表按 70 个官方分类翻页 —— **分类真的生效**，每类可挖到 offset≈1500~3000；
  · `v6/playlist/detail` 的 `tracks` 只回 10 条，但 **`trackIds` 是完整的**（实测 740/740）；
  · `v3/song/detail` 支持 **一次 1000 个 id**（实测 974 命中 / 3.7s）；
  · 所以主干是两段式：歌单 → 全量 trackIds → 批量取元数据。1 个歌单不再限 200 首。

四个可重入子命令：
  enum   枚举歌单池        → data/catalog/_playlists.json
  ids    拉全量 trackIds   → data/catalog/_ids.json
  songs  批量取元数据      → data/catalog/_songs.jsonl
  pack   归一+去重+分片    → data/catalog/manifest.json / shard-NNN.json / search.json
"""
import json, os, sys, time, ssl, re, traceback, urllib.request, urllib.error, urllib.parse, random
from concurrent.futures import ThreadPoolExecutor, as_completed

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CAT = os.path.join(ROOT, "data", "catalog")
PL_FILE = os.path.join(CAT, "_playlists.json")
IDS_FILE = os.path.join(CAT, "_ids.json")
SONGS_FILE = os.path.join(CAT, "_songs.jsonl")
ART_FILE = os.path.join(CAT, "_artist_ids.json")

CTX = ssl.create_default_context(); CTX.check_hostname = False; CTX.verify_mode = ssl.CERT_NONE
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
HDR = {"User-Agent": UA, "Referer": "https://music.163.com/",
       "Accept": "application/json, text/plain, */*", "Accept-Language": "zh-CN,zh;q=0.9"}

WORKERS = int(os.environ.get("HV_WORKERS") or "8")
PAGES = int(os.environ.get("HV_PAGES") or "12")
# ★ 2026-10-06 修正（实测证据）：原 MIN_PL_TRACKS=50 会把「陈奕迅精选 35 首」这类
#   高质量歌单整条过滤掉，只留下曲目数最多的「有声书合辑 / 素材库」（Bookstream 一家 1800 首）。
#   降到 20 后热门精选歌单才进得来 —— 热门池实测热度≥40 占比 73%（旧池仅 10%）。
MIN_PL_TRACKS = int(os.environ.get("HV_MIN_TRACKS") or "20")
MAX_PLAYLISTS = int(os.environ.get("HV_MAX_PLAYLISTS") or "50000")
# 参与枚举的排序：hot=按播放量（真热门歌单），new=最新（兜底新鲜度）
ENUM_ORDERS = [o for o in (os.environ.get("HV_ENUM_ORDERS") or "hot").split(",") if o]
# ★ 同上：必须先按 playCount 选，再按曲目数；旧的 (-n, -play) 等于优先选垃圾合辑
SORT_BY_PLAY = (os.environ.get("HV_SORT") or "play").lower() == "play"
TARGET_IDS = int(os.environ.get("HV_TARGET_IDS") or "600000")

# =============================================================== 多源扩充（★ 2026-10-07）
# 主人第五轮质问：「我就没明白是不能做聚合吗？为什么就死盯着网易呢！全网什么叫全网？」
#
# 实测结证（2026-10-07 本地跑，全部有数据）：
#   · 六个源**搜索接口全部匿名可用**：wy 220ms / kw 148ms / migu 527ms / bili 482ms
#     / qq 2557ms / kg 256ms，各回 20 条。之前探针报的 needs_auth 只是**播放直链**要登录，
#     跟「曲库采集」是两条路 —— 所以「不能聚合」这个前提根本不成立。
#   · 真正的差别在**搜索质量**（同一 query「晴天 周杰伦」第 1 条）：
#       QQ    晴天 / 周杰伦                        ✅ 干净
#       酷狗  晴天 / 周杰伦                        ✅ 干净
#       咪咕  晴天 / 周杰伦                        ✅ 干净
#       网易  晴天(深情版) / Lucky小爱              ❌ 翻唱压原唱（主人抱怨的原样）
#       酷我  20 条里没有一条原版（KTV伴奏/DJ版/串烧占满）  ❌❌ 只能当直链解析器
#   · 音源优先级仍保持 wy → kw → migu（wy 音质最好；kw 匿名直链最稳；migu 补免费库）。
#   · 旧流程为什么只盯网易：harvest 主干写死「网易云歌单」，重入子命令只有
#     enum/ids/songs/pack/stat/verify/wash —— **压根没有任何多源入口**；
#     adapters 里那套 QQ 榜单/64 分类歌单接口早就写好了，却只被 build.py 用来生成
#     categories/charts/streams，**从未进过曲库采集**。这是流程没接，不是技术不行。
MULTI_FILE = os.path.join(CAT, "_multi.jsonl")
# ★ 2026-10-07 新增：**已尝试**台账（含失败）。
#   为什么必须有：skip 集原先 = 网易池 + _multi.jsonl（只含**成功**记录），于是
#   「失败候选」永远留在队列头部、每轮都被重新试一遍。命中率 ~1/3 时后果是：
#   第 N 轮的 8000 条预算里绝大部分是已知失败，真正的新候选只能挤进去一点点，
#   候选池**尾部永远轮不到** —— 采集量会一路衰减到近乎停滞（典型的静默失效）。
#   现在按「最近是否试过」跳过，失败也有冷却期，队列才真正向前推进。
MULTI_TRIED = os.path.join(CAT, "_multi_tried.jsonl")
MULTI_RETRY_DAYS = int(os.environ.get("HV_MULTI_RETRY_DAYS") or "14")   # 失败多久后可重试
MULTI_ON = (os.environ.get("HV_MULTI") or "1") != "0"
MULTI_MAX = int(os.environ.get("HV_MULTI_MAX") or "20000")       # 单轮最多补多少条
# ★ 候选池上限必须**远大于** MULTI_MAX：否则第一个榜单就把池子填满，
#   后面 QQ 歌单/酷狗榜根本没机会收（实测血案：HV_MULTI_MAX=60 时 QQ 榜单占满 74 条 →
#   歌单收集直接 break，日志显示「0 分类 / 0 歌单」）。MULTI_MAX 只用来截断**待处理队列**。
MULTI_POOL = int(os.environ.get("HV_MULTI_POOL") or str(max(MULTI_MAX * 4, 20000)))
MULTI_WORKERS = int(os.environ.get("HV_MULTI_WORKERS") or "8")
MULTI_PL_PER_CAT = int(os.environ.get("HV_MULTI_PL_PER_CAT") or "4")      # 每个 QQ 分类取几个歌单
MULTI_SONGS_PER_PL = int(os.environ.get("HV_MULTI_SONGS_PER_PL") or "40")
# ★ 2026-10-07 扩容：QQ 分类歌单支持多个 sort 维度（1/2/3/5 = 最新/最热/… 不同榜，
#   歌单集合几乎不重叠）。原来只取 sort=3，全池仅 ~1 万条候选，而 CI 每轮能试 8000 条、
#   命中 ~39% → **两三轮就把池子抽干**，采集量随即断崖。加上排序维度 + 每分类歌单数，
#   池子直接上一个数量级。
MULTI_QQ_SORTS = os.environ.get("HV_MULTI_QQ_SORTS") or "1,2,3"
MULTI_KG_RANKS = int(os.environ.get("HV_MULTI_KG_RANKS") or "24")         # 取几个酷狗榜
MULTI_KG_PER_RANK = int(os.environ.get("HV_MULTI_KG_PER_RANK") or "50")
MULTI_QQ_PER_RANK = int(os.environ.get("HV_MULTI_QQ_PER_RANK") or "100")


def log(*a):
    print(*a, flush=True)


def get(url, data=None, timeout=25, retry=3):
    for i in range(retry):
        try:
            h = HDR if data is None else {**HDR, "Content-Type": "application/x-www-form-urlencoded"}
            req = urllib.request.Request(url, data=data, headers=h)
            with urllib.request.urlopen(req, timeout=timeout, context=CTX) as r:
                return json.loads(r.read().decode("utf-8", "replace"))
        except Exception as e:
            if i == retry - 1:
                return {"__err__": "%s: %s" % (type(e).__name__, e)}
            time.sleep(0.5 * (i + 1) + random.random() * 0.3)
    return None


# ---------------------------------------------------------------- enum
def _enum_page(job):
    sub, order, off = job
    d = get("https://music.163.com/api/playlist/list?cat=%s&order=%s&limit=60&offset=%d"
            % (urllib.parse.quote(sub), order, off))
    if not d or d.get("__err__"):
        return []
    out = []
    for p in (d.get("playlists") or []):
        pid, tc = p.get("id"), (p.get("trackCount") or 0)
        if pid and tc >= MIN_PL_TRACKS:
            out.append({"id": pid, "n": tc, "name": (p.get("name") or "")[:60],
                        "cat": sub, "play": p.get("playCount") or 0})
    return out


def cmd_enum():
    os.makedirs(CAT, exist_ok=True)
    cat = get("https://music.163.com/api/playlist/catalogue") or {}
    subs = []
    for s in (cat.get("sub") or []):
        n = s.get("name") if isinstance(s, dict) else str(s)
        if n and n not in subs:
            subs.append(n)
    only = os.environ.get("HV_CATS")
    if only:
        want = [x.strip() for x in only.split(",") if x.strip()]
        subs = [s for s in subs if s in want] or want
    log("分类数 =", len(subs), "排序 =", ENUM_ORDERS)

    jobs = [(sub, order, off) for sub in subs for order in ENUM_ORDERS
            for off in range(0, PAGES * 60, 60)]
    log("枚举页数 = %d" % len(jobs))
    seen, pls = set(), []
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        for k, fut in enumerate(as_completed([ex.submit(_enum_page, j) for j in jobs]), 1):
            for p in fut.result():
                if p["id"] in seen:
                    continue
                seen.add(p["id"])
                pls.append(p)
            if k % 200 == 0:
                log("  页 %d/%d  歌单 %d  %.0fs" % (k, len(jobs), len(pls), time.time() - t0))
            if len(pls) >= MAX_PLAYLISTS:
                break

    # 与已有池合并（不丢历史歌单，其 trackIds 已入库）
    old = {}
    if os.path.exists(PL_FILE):
        try:
            for p in json.load(open(PL_FILE, encoding="utf-8")).get("playlists") or []:
                old[p["id"]] = p
        except Exception:
            pass
    for p in pls:
        old.setdefault(p["id"], p)
    hist_cnt = len(old)
    merged = list(old.values())
    merged.sort(key=(lambda x: (-x.get("play", 0), -x.get("n", 0))) if SORT_BY_PLAY
                else (lambda x: (-x.get("n", 0), -x.get("play", 0))))

    plays = sorted(p.get("play", 0) for p in merged)
    q = lambda p: plays[min(len(plays) - 1, int(len(plays) * p))] if plays else 0
    json.dump({"updated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
               "count": len(merged), "sortedBy": "play" if SORT_BY_PLAY else "tracks",
               "playlists": merged},
              open(PL_FILE, "w", encoding="utf-8"), ensure_ascii=False)
    log("歌单池 %d 个（本轮枚举 %d，历史 %d）曲目理论上限 %d"
        % (len(merged), len(pls), hist_cnt, sum(p["n"] for p in merged)))
    log("播放量分位 p10=%d p50=%d p90=%d ｜ 排序依据=%s ｜ %.0fs"
        % (q(.1), q(.5), q(.9), "播放量" if SORT_BY_PLAY else "曲目数", time.time() - t0))


# ---------------------------------------------------------------- ids
def _ids_one(pid):
    d = get("https://music.163.com/api/v6/playlist/detail?id=%d&n=1000&s=8" % pid, timeout=30)
    if not d or d.get("__err__"):
        return pid, [], (d or {}).get("__err__")
    p = d.get("playlist") or {}
    out = []
    for t in (p.get("trackIds") or []):
        tid = t.get("id") if isinstance(t, dict) else t
        if tid:
            out.append(int(tid))
    return pid, out, None


def cmd_ids():
    pl = json.load(open(PL_FILE, encoding="utf-8"))["playlists"]
    store = json.load(open(IDS_FILE, encoding="utf-8")) if os.path.exists(IDS_FILE) else {"playlists": {}}
    got = store.setdefault("playlists", {})
    todo = [p for p in pl if str(p["id"]) not in got]
    log("待拉 trackIds 的歌单 %d（已有 %d 个）" % (len(todo), len(got)))
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futs = [ex.submit(_ids_one, p["id"]) for p in todo]
        for k, fut in enumerate(as_completed(futs), 1):
            pid, ids, err = fut.result()
            got[str(pid)] = ids if not err else []
            if k % 200 == 0:
                uniq = len({i for v in got.values() for i in v})
                log("  %d/%d  唯一 id %d  %.0fs" % (k, len(todo), uniq, time.time() - t0))
                json.dump(store, open(IDS_FILE, "w", encoding="utf-8"))
    json.dump(store, open(IDS_FILE, "w", encoding="utf-8"))
    uniq = {i for v in got.values() for i in v}
    log("trackIds 完成：%d 个歌单，唯一曲目 id %d（去重率 %.0f%%）"
        % (len(got), len(uniq), 100.0 * len(uniq) / max(1, sum(len(v) for v in got.values()))))


# ---------------------------------------------------------------- songs
def _songs_batch(ids):
    body = "c=" + urllib.parse.quote(json.dumps([{"id": i} for i in ids], separators=(",", ":")))
    d = get("https://music.163.com/api/v3/song/detail", data=body.encode(), timeout=60)
    if not d or d.get("__err__"):
        return [], set(), (d or {}).get("__err__")
    out, aids = [], set()
    for t in (d.get("songs") or []):
        sid = t.get("id")
        if not sid:
            continue
        ars = [a for a in (t.get("ar") or []) if a.get("name")]
        # 顺手收歌手 id —— 下一步「按歌手扩库」要用（歌手页最多再给 200 首/人）
        for a in ars:
            if a.get("id"):
                aids.add(int(a["id"]))
        ar = "/".join(a.get("name", "") for a in ars)
        al = t.get("al") or {}
        # ★ 质量三维（同一请求白送，不用额外抓）：
        #   pop  = 热度 0~100（筛选「没热度的烂歌」的主判据）
        #   pt   = 发行时间（ms）→ 「新歌顶掉老歌」
        #   nrc  = 无版权下架推荐标记（非空 = 该曲已不可播/被替换）
        out.append({"i": sid, "n": (t.get("name") or "").strip(), "a": ar.strip(),
                    "b": (al.get("name") or "").strip(),
                    "p": (al.get("picUrl") or "").replace("http://", "https://"),
                    "d": t.get("dt") or 0,
                    "f": t.get("fee") if t.get("fee") is not None else -1,
                    "pop": int(t.get("pop") or 0),
                    "pt": int(t.get("publishTime") or 0),
                    "nrc": 1 if t.get("noCopyrightRcmd") else 0,
                    # ★ 原唱标记（同一请求白送）：0 原创 / 1 其他版本 / 2 翻唱
                    #   ov = 翻唱所指向的原唱 songId（原唱线索，供下一轮扩库）
                    "oct": int(t.get("originCoverType") or 0),
                    "ov": int((t.get("originSongSimpleData") or {}).get("songId") or 0)})
    return out, aids, None


def cmd_songs():
    store = json.load(open(IDS_FILE, encoding="utf-8"))
    pool = (list(store.get("playlists", {}).values())
            + list(store.get("artists", {}).values())
            + [store.get("origin") or []])          # ★ 翻唱指出的原唱，一并取元数据
    ids = sorted({i for v in pool for i in v})
    done = set()
    if os.path.exists(SONGS_FILE):
        for line in open(SONGS_FILE, encoding="utf-8"):
            try:
                for s in json.loads(line).get("songs") or []:
                    done.add(s["i"])
            except Exception:
                pass
    todo = [i for i in ids if i not in done]
    log("曲目 id 共 %d，待取 %d（已有 %d）" % (len(ids), len(todo), len(done)))

    B = 1000
    batches = [todo[i:i + B] for i in range(0, len(todo), B)]
    t0, n = time.time(), 0
    aids = set()
    if os.path.exists(ART_FILE):
        aids = set(json.load(open(ART_FILE, encoding="utf-8")))
    f = open(SONGS_FILE, "a", encoding="utf-8")
    with ThreadPoolExecutor(max_workers=min(8, WORKERS)) as ex:
        futs = [ex.submit(_songs_batch, b) for b in batches]
        for k, fut in enumerate(as_completed(futs), 1):
            songs, a2, err = fut.result()
            if err:
                f.write(json.dumps({"_err": err}, ensure_ascii=False) + "\n")
            else:
                f.write(json.dumps({"songs": songs}, ensure_ascii=False) + "\n")
                n += len(songs)
                aids |= a2
            if k % 10 == 0:
                f.flush()
                json.dump(sorted(aids), open(ART_FILE, "w", encoding="utf-8"))
                log("  批 %d/%d  累计 %d 首 · 歌手 %d  %.0fs" % (k, len(batches), len(done) + n, len(aids), time.time() - t0))
    f.close()
    json.dump(sorted(aids), open(ART_FILE, "w", encoding="utf-8"))
    log("元数据完成：本批 %d 首 · 歌手 %d 位 → %s" % (n, len(aids), SONGS_FILE))


# ---------------------------------------------------------------- artists
def _artist_songs(aid):
    """歌手页：一次最多给 200 首（limit 再大也只回 200），翻两页凑 ~400"""
    out = []
    for off in (0, 200, 400):
        d = get("https://music.163.com/api/v1/artist/songs?id=%d&limit=200&offset=%d&order=hot" % (aid, off), timeout=30)
        if not d or d.get("__err__"):
            break
        arr = d.get("songs") or []
        if not arr:
            break
        for s in arr:
            if s.get("id"):
                out.append(int(s["id"]))
        if len(arr) < 200:
            break
    return aid, out


def cmd_artists():
    MAXA = int(os.environ.get("HV_MAX_ARTISTS") or "0")
    # ★ 冷启动防御：CI 的 actions/cache 从未命中过（实测 Cache not found），
    #   每轮都是全套重采。此时 _artist_ids.json 由上一步 songs 现写现用；
    #   万一 songs 没跑或没产出，这里也不能直接崩 —— 空池就当无事发生。
    aids = json.load(open(ART_FILE, encoding="utf-8")) if os.path.exists(ART_FILE) else []
    if not aids:
        log("按歌手扩库：_artist_ids.json 为空（冷启动首轮属正常）→ 跳过")
        return
    store = json.load(open(IDS_FILE, encoding="utf-8"))
    done = store.setdefault("artists", {})
    base = {i for v in store.get("playlists", {}).values() for i in v}
    todo = [a for a in aids if str(a) not in done]
    if MAXA:
        todo = todo[:MAXA]
    log("按歌手扩库：歌手 %d 位，待拉 %d（已有 %d）· 基线唯一 id %d" % (len(aids), len(todo), len(done), len(base)))
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futs = [ex.submit(_artist_songs, a) for a in todo]
        try:
            for k, fut in enumerate(as_completed(futs), 1):
                aid, ids = fut.result()
                done[str(aid)] = ids
                base.update(ids)
                if k % 300 == 0:
                    json.dump(store, open(IDS_FILE, "w", encoding="utf-8"))
                    log("  %d/%d  唯一曲目 id %d  用时 %.0fs" % (k, len(todo), len(base), time.time() - t0))
                if len(base) >= TARGET_IDS:
                    log("  ★ 唯一曲目 id 已达 %d，停止剩余歌手" % TARGET_IDS)
                    for x in futs:
                        x.cancel()
                    break
        finally:
            json.dump(store, open(IDS_FILE, "w", encoding="utf-8"))
    json.dump(store, open(IDS_FILE, "w", encoding="utf-8"))
    log("歌手扩库完成：%d 位歌手 · 曲目 id %d 条"
        % (len(done), sum(len(v) for v in done.values())))


# ---------------------------------------------------------------- pack
# ── 质量优先（主人 2026-10-05 钦定）────────────────────────────────────────
# 「数量不是关键，质量和热度才是真的关键点」+「新歌替换没有热度的烂歌」。
# 所以这里不是"能捞多少算多少"，而是**先打分再截断**：
#   score = pop(热度 0~100，网易云自己算的) + 新鲜度加成(publishTime)
# 再把尾部低分的整批丢掉 —— 宁可 20 万首首首能听，也不要 60 万首一半是死水。
SKIP_FEE = {1, 4}       # 1=VIP 专享、4=需购买专辑：匿名不可播，不入库
SHARD = 1000            # 每片 1000 首（按需拉取粒度）
IDX_CHUNK = 40000       # 每个索引块 4 万行
SEP = "\u0001"          # 行内分隔符（不可打印，不会出现在歌名里）
KEEP = int(os.environ.get("HV_KEEP") or "260000")   # 只保留评分最高的 N 首
# ★ 2026-10-06 实测：热度地板必须 >0，否则灌进来的是「有声书/播客/白噪音/素材库」深水。
#   实测 pop=5~9 的 8 万首抽样：Bookstream 有声书一家 1800 首、Power Yoga Nature Sounds、
#   X-Ray Dog 素材库 1571 首 —— 无一是歌。地板 10 时抽样 20 首全为真歌（林忆莲/甄妮/Lenka…）。
MIN_POP = int(os.environ.get("HV_MIN_POP") or "10")  # 热度硬地板
MIN_DUR = 30000         # 30 秒以下多为过场/语音，不是歌
MAX_DUR = int(os.environ.get("HV_MAX_DUR") or "900000")   # 15 分钟以上多为组曲/有声书
NAME_MAX = 80           # 歌名超长的多是「曲目+选段+指挥+乐团」全堆一块的灌水
ARTIST_MAX = 60

# ===== 质量闸门（2026-10-05 真机血案：31% 翻唱垃圾混进曲库，主人怒斥"不会唱歌的也进来了"）=====
# 翻唱/伴奏/DJ 版/铃声/助眠白噪音/清唱哼唱……一律不要；
# 实力歌手（候选池里有 >= STRONG_N 首 pop>=30 的歌）全碟收录（pop>=POP_EST），
# 陌生歌手必须 pop>=POP_NEW（爆款才给进门）——宁缺毋滥。
# 2026-10-06 追加「非歌曲内容」一档：有声书/播客/朗读/电台剧/瑜伽冥想/素材库配乐。
#   证据：线上 13 万曲库 61% 是 pop=5~9，抽样 25 首里 20 首是德语有声书
#   （Kapitel 1: … Teil 188 — Audio Media Digital Hörbücher）与助眠/冥想音轨；
#   而 pop≥70 抽样 15 首全是真歌（周笔畅/山下達郎/Olivia Rodrigo）——所以非歌曲必须单独挡。
JUNK_RE = re.compile(
    # ★ 2026-10-06 补两处漏网（实测点名），但都收窄过，避免误杀：
    #   · dj —— 只剔「作为后缀出现」的 dj（光年之外dj / xxx dj版），
    #     不能裸匹配：Dj Snake、DJ Got Us Fallin In Love 是正版艺人/作品（实测会误杀）
    #     → 用 (?<=[\u4e00-\u9fa5\s])dj(?:版|版|mix)?\s*$ 只认中文名尾部
    #   · 器乐改编版（宿命洞箫版 原本漏网）—— 换乐器演奏不是原唱，
    #     且歌词时间轴对不上（用户抱怨的「词不对版」来源之一）
    r"翻唱|cover|伴奏|instrumental|抖音|铃声|纯音乐|钢琴版|吉他版|尤克里里|八音盒|"
    r"口琴|陶笛|葫芦丝|萨克斯|二胡|古筝|电子琴|哼唱|清唱|翻自|ktv|慢速|加速|降调|升调|"
    r"remix|八轨|和声版|消音|立体声环绕|睡眠|白噪音|胎教|助眠|asmr|钢琴曲|轻音乐|"
    r"洞箫|笛子版|琵琶版|箫版|筝版|埙|笙版|唢呐|扬琴|马头琴|手风琴|"
    r"audiobook|audio\s*book|bookstream|朗读|有声书|有声剧|播客|podcast|广播剧|"
    r"chapter\s*\d|kapitel\s*\d|teil\s*\d|episode\s*\d|电台剧|朗读版|"
    r"nature\s*sound|yoga|meditation|spa\s*music|white\s*noise|rain\s*sound|"
    r"素材|production\s*music|trailer\s*music|背景音乐|彩铃", re.I)
# ★ 2026-10-06 主人点名「不要有演唱会那种的」→ 单独一条 live/现场闸。
#   为什么单独写、收得这么紧：LIVE 是个**正常英文词**（Live Forever / Live and Learn
#   是正经作品），裸匹配 live 会误杀。所以只认三类**明确是"某场演出"的信号**：
#     ① 中文场景词：演唱会 / 现场版 / 音乐会 / 音乐节 / 歌友会 / 巡回 / 巡演 / 见面会
#     ② 「(Live)」「（现场）」这种**带括号的版本标注**（Spotify/网易云的标准做法）
#     ③ 中文名尾部的 live（孤勇者live）
#   注意：`(Live)` 必须带括号 —— Rihanna《Live Your Life》不带括号不会中招。
LIVE_RE = re.compile(
    r"演唱会|现场版|音乐会|音乐节|歌友会|巡回|巡演|见面会|跨年|春晚|颁奖|盛典|"
    r"[（(\[【][^）)\]】]{0,10}(?:live|现场)[^）)\]】]{0,10}[）)\]】]|"
    r"[\u4e00-\u9fa5]\s*(?:live|现场)\s*$", re.I)
# dj 收紧版：只认「中文名尾部」的 dj（光年之外dj / 孤勇者 dj版），放过 Dj Snake 这类正版艺人
JUNK_DJ_RE = re.compile(r"[\u4e00-\u9fa5]\s*dj\s*(?:版|mix|remix)?\s*$", re.I)


def is_junk(*fields):
    """统一垃圾判定：JUNK_RE + LIVE_RE + 收紧版 dj。所有调用点都走这里，避免新增规则漏改。"""
    for s in fields:
        s = s or ""
        if JUNK_RE.search(s) or LIVE_RE.search(s) or JUNK_DJ_RE.search(s):
            return True
    return False
STRONG_N = int(os.environ.get("HV_STRONG_N") or "5")
POP_EST = int(os.environ.get("HV_POP_EST") or "10")
POP_NEW = int(os.environ.get("HV_POP_NEW") or "40")


# ============================================================ 音频级可播闸门
# ★ 2026-10-06 主人口述「好多歌时长不够还不是原唱」「必须解决」后加的最后一闸。
#
# 为什么元数据闸门（上面那些）不够用 —— 三条实测铁证：
#   1) **fee 字段不可信**。周杰伦《稻香》i=185709 fee=0（标称免费），outer 实际回
#      4515 字节 HTML；《烟花易冷》《青花瓷》《夜曲》同批 33 首里 28 首如此。
#   2) **VIP 试听片段伪装成真音频**。伍佰《泪桥》i=156736 回 481115 字节的
#      `audio/mpeg`（Content-Type 完全正常），前 2KB 也是标准 `\xff\xfb` MP3 帧头 ——
#      **二进制嗅探 100% 认不出**。实为 30 秒试听（原曲 225 秒）。
#   3) **唯一可靠判据 = Content-Length 反算时长**。
#      免费完整曲：Samson et Dalila 302s→302s、Calm and Relaxed 155s→155s（误差 0 秒）；
#      试听片段：泪桥 481115B / 225s = **17 kbps**（正常 128 kbps）。
#
# 做法对齐业界（已核对源码）：
#   · lx-music  `isEqualsInterval`: |目标时长-候选时长| <= 5 秒才放行；
#   · spotDL    `calc_time_match`: exp(-0.1*Δ)，time_match<25 直接淘汰；
#   · UnblockNeteaseMusic: 「读前 8KB 验码率剔除死链」。
# 这里取等价且更省流量的形式：HEAD 拿 Content-Length → 反算实际码率 → 落在
# 合理区间才算「真的是这首完整曲」。全程不发 GET，不下载音频。
AUDIO_GATE = (os.environ.get("HV_AUDIO_GATE") or "1") != "0"
AUDIO_WORKERS = int(os.environ.get("HV_AUDIO_WORKERS") or "24")
AUDIO_MIN_BYTES = 20000          # 低于此必是 HTML 错误页/占位符
# ★ 2026-10-06 实测修正：原阈值 70 kbps 是**误杀源**。
#   匿名实测（15 首热歌）发现网易云免签外链存在三类响应：
#     · 3.3~5.2MB / audio/mpeg  = 128k 完整版      → keep
#     · 481115 / 720813 bytes   = **低码率完整版**（11~22 kbps）→ 也是能听的真歌！
#     · 4515 / text/html        = 不可播（HTML 下载页）→ dead
#   旧逻辑用 70kbps 卡，会把中间那类**低码率正常歌整片误判成「试听片段」剔掉**。
#   而「试听片段」的本质不是码率低，是**字节数装不下它宣称的时长**。
#   故改判据：由 (字节/码率区间) 改为 (字节能覆盖的时长 vs 元数据时长的比例)。
# ★★★ 2026-10-07 修正：原值 8 让覆盖率判据**形同虚设**。
#   数学上讲：cover = kbps / AUDIO_KBPS_LO，取 LO=8 就等于「kbps ≥ 6 即放行」——
#   真正的试听片段（481115B 冒充 225s = 17kbps）被算成 cover=2.14 ≥ 0.75 而**放行**。
#   上线实测（本地按体检同 seed 抽 240 首）：库里 kbps<70 的只剩 4 首「英语听力」
#   （32.1/64.1/64.3/64.6 kbps），全库 p05=p50=128.0 kbps —— 说明旧注释里说的
#   「11~22kbps 的正常歌」在真实数据里**并不存在**；那批其实是 30s 试听片段，
#   同一个 481115B/225s 的案例，catalog_verify.py 第 139 行早已明确判它是 VIP 试听片段。
#   → 取 32kbps 作分母：既能抓 17kbps 级真片段（cover=17/32=0.53 < 0.75），
#     又不误杀 32kbps 以上的低码率实录（听力录音/老唱片翻录天然就在 32~64kbps）。
AUDIO_KBPS_LO = int(os.environ.get("HV_KBPS_LO") or "32")    # 「片段」判据的分母：覆盖率 = kbps / 此值
AUDIO_KBPS_HI = 450              # 320k/无损上限
AUDIO_COVER_MIN = float(os.environ.get("HV_CLIP_COVER") or "0.75")  # 字节能覆盖的时长 < 元数据时长*0.75 → 判试听片段
AUDIO_MAX_PER_RUN = int(os.environ.get("HV_AUDIO_MAX") or "0")   # 0=不限
AUDIO_CACHE_FILE = os.path.join(CAT, "_audio_gate.json")
# ★★ 2026-10-08 血案修复（可播判定的**有效期**）：
#   缓存原本只按 song id 查、**永不过期** → 老曲目测过一次 keep 就永久放行，
#   链接后来死了也照样发布。而体检脚本（catalog_verify.py）**每次真测** →
#   线上真实可播率被自己压低，harvest 被自己的闸门卡死：
#   run 37736589880（2026-10-08）实测可播率 97.9% < 98%，**整批 79,457 首被阻断**，
#   线上停在旧的 68,903 首。
#   现在：缓存带时间戳，超过 TTL 一律重测。老缓存（无 ts）视为已过期 → 首发轮会全量重测一次。
AUDIO_TTL_H = float(os.environ.get("HV_AUDIO_TTL_H") or "168")   # 小时，默认 7 天
AUDIO_TTL_S = AUDIO_TTL_H * 3600.0                               # 0 = 每轮全量重测
OUTER_HDR = {"User-Agent": ("Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) "
                            "AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148"),
             "Referer": "https://music.163.com/"}


def _outer_bytes(sid, timeout=8, retry=2):
    """HEAD 网易云免签外链，返回 (bytes, ctype)。

    ★ 2026-10-06 血案修复（云端把 5.6 万首砍到 1.6 万，剔掉 4 万"死链"）：
      旧实现只读 Content-Length，且把 **任何 4xx 的 body 长度** 当成真实字节数返回；
      云端 runner 的 IP 一旦被网易云风控，HEAD 会被成批拒掉 —— 那些 4xx 的错误页
      长度往往很小，于是被判成「死链」，把**本来能播的歌整片误杀**。
      病根有二，一并修：
        ① 4xx 不再当作「确定死链」，而是当作「测不到」（-1）→ 上层放行（宁可不治不能错治）；
        ② 网易云对**不可播**的歌会 302 到 HTML 下载页并返回 **200 + text/html**，
           仅凭「长度 < 20000」是巧合式判据（错误页一旦变大就会放行死歌）。
           这里改为**看 Content-Type**：真音频是 audio/*，HTML 一律判 dead —— 语义级判据。
    """
    url = "https://music.163.com/song/media/outer/url?id=%d.mp3" % int(sid)
    for i in range(retry):
        try:
            req = urllib.request.Request(url, headers=OUTER_HDR, method="HEAD")
            with urllib.request.urlopen(req, timeout=timeout, context=CTX) as r:
                cl = r.headers.get("Content-Length")
                ct = (r.headers.get("Content-Type") or "").lower()
                return (int(cl) if cl else 0), ct
        except urllib.error.HTTPError as e:
            # 4xx/5xx = 被拒/风控，**不是**「这首歌是死链」，交给上层当 unknown 放行
            return -1, ""
        except Exception:
            if i == retry - 1:
                return -1, ""
            time.sleep(0.25 + random.random() * 0.25)
    return -1, ""


def _load_audio_cache():
    try:
        with open(AUDIO_CACHE_FILE, encoding="utf-8") as fh:
            d = json.load(fh)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _save_audio_cache(d):
    try:
        os.makedirs(CAT, exist_ok=True)
        tmp = AUDIO_CACHE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(d, fh, separators=(",", ":"))
        os.replace(tmp, AUDIO_CACHE_FILE)
    except Exception as e:
        log("  ! 可播缓存写入失败：%s" % e)


def _verdict(cl, dur_ms, ctype=""):
    """判定 keep / dead / clip / unknown。

    判据（按可靠性从高到低）：
      ① Content-Type：text/html / application/json = 网易云的「不可播」下载页 → dead
         （★ 语义级判据。实测该页固定 200 + 4515 字节，只靠长度判是巧合式的）
      ② 字节数装不下宣称时长 → clip（真正意义上的「试听片段」）
      ③ 字节数 < 20KB → dead（残缺/占位）
      ④ 测不到（-1）或 0 字节旧缓存 → unknown（放行，宁可不治不能错治）
    """
    if cl is None or cl < 0:
        return "unknown"
    ct = (ctype or "").lower()
    # 旧缓存兼容：旧版 _outer_bytes 遇到 4xx 会返回 0，而 0 正是「风控误杀」的产物。
    # 这类「0 字节且无 Content-Type」的记录不可信，一律当测不到放行，绝不据此剔歌。
    if cl == 0 and not ct:
        return "unknown"
    if "text/html" in ct or "application/json" in ct:
        return "dead"                      # 明确的错误页/占位页：判死
    if ct and not ct.startswith("audio/"):
        return "unknown"                   # 非 audio 也非已知错误页 → 测不准，放行
    if cl < AUDIO_MIN_BYTES:
        return "dead"
    dur = (dur_ms or 0) / 1000.0
    if dur <= 0:
        return "keep"                      # 没有时长元数据 → 无从比对，放行
    kbps = cl * 8 / dur / 1000.0
    if kbps > AUDIO_KBPS_HI:
        return "keep"                      # 异常高码率（无损/多轨）不受下界约束
    # ★ 试听片段的本质：字节数只够放「宣称时长的一部分」。
    #   用最低可信码率（AUDIO_KBPS_LO）反算「这些字节能撑多久」，与宣称时长比。
    cover = (cl * 8 / AUDIO_KBPS_LO / 1000.0) / dur
    return "keep" if cover >= AUDIO_COVER_MIN else "clip"


def audio_gate(arr, tag="pack"):
    """对 arr（曲目 dict 列表）做音频级可播体检，返回 (通过的曲目, 统计明细)。

    结果按 song id 缓存到 _audio_gate.json，重跑只补新歌 → 增量且可中断续跑。
    """
    if not AUDIO_GATE:
        return arr, {"音频闸门": "已跳过"}
    cache = _load_audio_cache()
    # ★ 2026-10-07 多源免检（血案预防）：
    #   _outer_bytes() 探的是**网易**的 outer/url，多源曲目（咪咕 contentId / 酷我 rid / bvid）
    #   套这个 URL 必然探错对象 —— 运气好回 4xx → 判「测不到」放行；运气差回 200+text/html
    #   → 被当**死链直接删掉**。而它们在采集阶段已经过 adapters.verify_playable()
    #   （Range GET 拿真音频字节，唯一判据）实测可播。所以：不探、不缓存、原样放行。
    wy = [s for s in arr if (s.get("src") or "wy") == "wy"]
    other = [s for s in arr if (s.get("src") or "wy") != "wy"]
    # ★ 2026-10-08：命中判定从「缓存里有」改为「缓存里有**且没过期**」。
    #   老缓存是 (bytes, ctype) 二元组、没有时间戳 → 一律视为过期 → 重测，
    #   这正是我们要的：把「上一代测过 keep、现在可能已死」的曲目重新验一遍。
    _now = time.time()

    def _fresh(i):
        e = cache.get(str(i))
        if e is None or AUDIO_TTL_S <= 0:
            return False
        ts = e[2] if isinstance(e, (list, tuple)) and len(e) > 2 else 0
        try:
            return (_now - float(ts)) < AUDIO_TTL_S
        except Exception:
            return False

    known = {str(s["i"]) for s in wy if _fresh(s["i"])}
    todo = [s for s in wy if str(s["i"]) not in known]
    if AUDIO_MAX_PER_RUN:
        todo = todo[:AUDIO_MAX_PER_RUN]
    log("音频可播体检：待测 %d 首（缓存命中 %d 首）｜多源免检 %d 首｜%d 并发"
        % (len(todo), len(known), len(other), AUDIO_WORKERS))
    t0 = time.time()
    if todo:
        done = 0
        with ThreadPoolExecutor(AUDIO_WORKERS) as ex:
            futs = {ex.submit(_outer_bytes, s["i"]): s for s in todo}
            for fut in as_completed(futs):
                s = futs[fut]
                try:
                    _cl, _ct = fut.result()
                except Exception:
                    _cl, _ct = -1, ""
                cache[str(s["i"])] = (_cl, _ct, time.time())   # ★ 带时间戳，供 TTL 判定
                done += 1
                if done % 5000 == 0:
                    left = (len(todo) - done) * (time.time() - t0) / max(1, done)
                    log("  … %d/%d 已测（剩约 %.0f 分钟）" % (done, len(todo), left / 60))
                    _save_audio_cache(cache)
        _save_audio_cache(cache)

    keep, stat = list(other), {"死链/占位": 0, "试听片段": 0, "网络未测": 0}
    drop_ids = []
    for s in wy:
        entry = cache.get(str(s["i"]))
        # 兼容旧缓存（纯 bytes）与新缓存（(bytes, ctype) 二元组）
        if isinstance(entry, (list, tuple)):
            cl = entry[0] if len(entry) > 0 else -1
            ct = entry[1] if len(entry) > 1 else ""
        else:
            cl, ct = entry, ""
        v = _verdict(cl, s.get("d"), ct)
        if v == "keep":
            keep.append(s)
        elif v == "dead":
            stat["死链/占位"] += 1
            drop_ids.append(s["i"])
        elif v == "clip":
            stat["试听片段"] += 1
            drop_ids.append(s["i"])
        else:
            stat["网络未测"] += 1
            keep.append(s)          # 测不到就放行（宁可不治也不能错治：别误杀）

    # ★ 2026-10-06 风控熔断：云端 IP 被网易云成批拒绝时，「网络未测」会占比极高。
    #   此时继续按 dead 剔歌是危险的（上一轮就是这样把 5.6 万砍到 1.6 万）。
    #   判据：未测占比 > 40% → 判定本轮探测不可信，**整体放行、不剔任何歌**。
    #   ★ 2026-10-07：分母改成「真正被探的网易曲目数」——多源曲目压根没探，
    #     放进分母会把比例稀释，导致风控熔断**该响的时候不响**（危险方向）。
    total = len(wy)
    if total and stat["网络未测"] / total > 0.40:
        log("[%s] ⚠ 风控熔断：网络未测 %d/%d（%.0f%%）超过 40%% → 本轮音频闸门整体放行，不剔除任何曲目"
            % (tag, stat["网络未测"], total, 100.0 * stat["网络未测"] / total))
        return arr, {"音频闸门": "风控熔断·整体放行", "网络未测": stat["网络未测"]}

    log("[%s] 音频闸门：%d → %d 首 ｜ 剔除 %d（%s）"
        % (tag, len(arr), len(keep), len(arr) - len(keep),
           "、".join("%s %d" % (k, v) for k, v in stat.items() if v)))
    if drop_ids:
        p = os.path.join(CAT, "_audio_dropped.txt")
        with open(p, "a", encoding="utf-8") as fh:
            for i in drop_ids:
                fh.write("%s\n" % i)
    return keep, stat


# ============================================================ 原唱/翻唱标注
# ★★ 2026-10-06 主人第三轮点名后的**方案反转**（前两轮我做错了，这里改正）：
#
#   主人原话：「你是真厉害原唱都删了只保留了个翻唱伍佰的还有很多都是这样」
#             「剩一个翻唱剩什么呵呵」
#             「**不是翻唱不能有 是不能和原唱混 原唱就是原唱翻唱就是翻唱**」
#
# 我上一轮的错法：把 oct=2（翻唱）**整条删掉**。后果是**灾难性的**：
#   · 伍佰《泪桥》原唱（id=156736/156356）恰好是 VIP 歌 → 被**音频闸门**判 dead 剔掉；
#     留下来能过的都是**免费翻唱** → 最后歌手页只剩翻唱，比不删还糟。
#   · 大量「歌单里本来就只有翻唱版」的歌，一删就是**整首歌消失**（用户原话「剩一个翻唱剩什么」）。
#
# 主人指出的正法：**翻唱可以有，但不能和原唱混**。
#   → 所以这里**不再删任何翻唱**，改成三件事：
#     ① 打标：每条曲目带 `cv`（1=原唱 / 2=翻唱 / 0=未知），索引里带上原唱指针；
#     ② 原唱回填：发现翻唱时，把它指的**原唱 id 拉进来**（ensure_oct 已经在攒 origin 池），
#        让「原唱本尊」和「翻唱」**同场竞争**而不是只剩翻唱；
#     ③ 排序：客户端按 (歌名|歌手) 聚合时 **oct=1 原唱置顶**，翻唱挂「翻唱」角标，
#        并显示「原唱：XXX」—— 主人要的「原唱就是原唱 翻唱就是翻唱」就落在这里。
#
# 判据仍用**网易云自己标的 originCoverType**（`v3/song/detail` 白送，零额外开销）：
#   oct=0 = 未知/原创 → cv=0（不标，也不降权）
#   oct=1 = 原曲/原唱 → cv=1（**置顶**）
#   oct=2 = 翻唱      → cv=2（**保留**，挂角标；ov = 它指向的原唱 songId）
#   oct=3 = 未知类型  → cv=0
#
# 名字闸 VER_RE 同样**只做标注不做删除**（live/伴奏/dj 版挂标降权，仍可搜到）。
OCT_FILE = os.path.join(CAT, "_oct.json")
OCT_WORKERS = int(os.environ.get("HV_OCT_WORKERS") or "8")
OCT_FILE = os.path.join(CAT, "_oct.json")
OCT_WORKERS = int(os.environ.get("HV_OCT_WORKERS") or "8")
VER_RE = re.compile(
    # ① 括号里的版本标记：允许标记前后各带一点修饰（「江苏卫视2015新年**演唱会**版」
    #    「合唱**伴奏**」「Cover 陆二胡」），但**必须落在括号内** —— 这样
    #    《Live Forever》《现场之王》这类正经原创不会被误杀（实测 2000 抽样误杀 0）
    r"[（(\[【][^）)\]】]{0,14}?"
    r"(?:live|现场|演唱会|音乐会|音乐节|歌友会|深情版|治愈版|女声版|男声版|"
    r"童声版|烟嗓版|温柔版|伤感版|女版|男版|纯享|合唱伴奏|伴奏|清唱|卡拉\s?ok|ktv|"
    r"翻唱|翻自|改编|remix|cover|acoustic|unplugged|demo|instrumental|karaoke|"
    r"dj版|慢摇|降调|升调|变速|倍速|加速版|减速版|钢琴版|吉他版|尤克里里|八音盒|"
    r"口琴版|葫芦丝|陶笛|二胡版|古筝版|纯音乐)"
    r"[^）)\]】]{0,14}?[）)\]】]"
    # ② 无括号但挂在名字结尾的版本标记（「夜曲 伴奏版」「你好 慢摇版」）
    r"|(?:live|现场版|演唱会版|音乐节版|深情版|治愈版|女声版|男声版|童声版|纯享版|"
    r"伴奏版|清唱版|翻唱版|dj版|慢摇版|降调版|升调版|二倍速|变速版)\s*$", re.I)


def _oct_load():
    try:
        with open(OCT_FILE, encoding="utf-8") as fh:
            d = json.load(fh)
        return {int(k): v for k, v in d.items()} if isinstance(d, dict) else {}
    except Exception:
        return {}


def _oct_save(m):
    try:
        os.makedirs(CAT, exist_ok=True)
        tmp = OCT_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({str(k): v for k, v in m.items()}, fh, separators=(",", ":"))
        os.replace(tmp, OCT_FILE)
    except Exception as e:
        log("  ! 原唱缓存写入失败：%s" % e)


def _oct_fetch(ids):
    """批量取 originCoverType / 原唱 id。一次 1000 个（实测 974 命中 / 3.7s）"""
    out = {}
    body = "c=" + urllib.parse.quote(json.dumps([{"id": i} for i in ids], separators=(",", ":")))
    d = get("https://music.163.com/api/v3/song/detail", data=body.encode(), timeout=60)
    if not d or d.get("__err__"):
        return out
    for t in (d.get("songs") or []):
        if not t.get("id"):
            continue
        ov = t.get("originSongSimpleData") or {}
        out[int(t["id"])] = [int(t.get("originCoverType") or 0), int(ov.get("songId") or 0)]
    return out


def ensure_oct(ids):
    """确保 ids 的 originCoverType 都已就位（增量缓存 _oct.json，可中断续跑）。

    顺手把「翻唱条目指出的原唱 id」记进 _ids.json['origin'] —— 那些原唱我们大多还没有，
    下一轮 songs 会连它们的元数据一起拉，用真原唱去顶掉翻唱，而不是简单删空了事。
    """
    m = _oct_load()
    if _OCT_SEED:
        m.update({k: v for k, v in _OCT_SEED.items() if k not in m})
    # ★ 2026-10-07：多源曲目混进来后，id 不再保证是数字 —— B 站是 "BV1xx…" 这种字母串，
    #   酷狗是 hex hash。原写 `[int(i) for i in …]` 会直接 ValueError 炸掉整个 pack。
    #   而且网易的 song/detail 本来就只认自家 songId，非数字 id 送了也是白送 → 直接跳过。
    want, skipped = [], 0
    for i in dict.fromkeys(ids):
        try:
            want.append(int(i))
        except (TypeError, ValueError):
            skipped += 1
    if skipped:
        log("  原唱体检：跳过 %d 个非数字 id（B 站 bvid / 酷狗 hash 等，网易接口不认）" % skipped)
    miss = [i for i in want if i not in m]
    log("原唱体检：待取 %d 首（缓存命中 %d 首）｜%d 并发" % (len(miss), len(want) - len(miss), OCT_WORKERS))
    if miss:
        t0, batches = time.time(), [miss[i:i + 1000] for i in range(0, len(miss), 1000)]
        with ThreadPoolExecutor(OCT_WORKERS) as ex:
            for k, r in enumerate(ex.map(_oct_fetch, batches), 1):
                m.update(r)
                if k % 10 == 0:
                    _oct_save(m)
                    log("  … %d/%d 批（已取 %d 首，%.0fs）" % (k, len(batches), len(m), time.time() - t0))
        _oct_save(m)

    # 翻唱指出的原唱 id → 攒进采集池，供下一轮拉元数据
    orig = {v[1] for v in (m.get(i) for i in want) if v and v[0] == 2 and v[1]}
    if orig:
        try:
            store = json.load(open(IDS_FILE, encoding="utf-8")) if os.path.exists(IDS_FILE) else {}
            have = set(store.get("origin") or [])
            add = sorted(orig - have)
            if add:
                store["origin"] = sorted(have | orig)
                json.dump(store, open(IDS_FILE, "w", encoding="utf-8"), separators=(",", ":"))
                log("原唱线索：新增 %d 个原唱 id 待采集（累计 %d）" % (len(add), len(store["origin"])))
        except Exception as e:
            log("  ! 原唱线索写入失败：%s" % e)
    return m


def original_gate(arr, tag="pack"):
    """给每条曲目**标注**原唱/翻唱（★ 不再删除任何一条）。

    返回 (标注后的 arr, 统计明细)。arr 元素需含 i / n / a。
    产出字段：`cv`（0 未知 / 1 原唱 / 2 翻唱）与 `ov`（翻唱指向的原唱 songId，无则不带）。

    为什么改成标注（血案复盘 2026-10-06）：
      上一版把 oct=2 直接 drop，结果伍佰《泪桥》**原唱是 VIP** 被音频闸门剔掉，
      免费翻唱反而留下 —— 主人看到的就是「原唱都删了只保留了个翻唱」。
      正确做法是「同场竞争 + 角标区分」，删除只会让翻唱变成唯一幸存者。
    """
    if (os.environ.get("HV_ORIGIN_GATE") or "1") == "0":
        return arr, {"原唱标注": "已跳过"}
    oct = ensure_oct([s["i"] for s in arr])
    stat = {"原唱": 0, "翻唱": 0, "未知": 0, "名字带版本标": 0}
    for s in arr:
        try:
            v = oct.get(int(s["i"])) or [0, 0]
        except (TypeError, ValueError):
            v = [0, 0]                       # 非数字 id（bvid/hash）→ 网易查不到，按未知处理
        t = int(v[0] or 0)
        if t == 1:
            s["cv"] = 1
            stat["原唱"] += 1
        elif t == 2:
            s["cv"] = 2
            if v[1]:
                s["ov"] = int(v[1])          # 原唱指针：客户端可显示「原唱：XXX」
            stat["翻唱"] += 1
        elif s.get("cv") == 1 and (s.get("src") or "wy") != "wy":
            # ★ 2026-10-07 多源：适配器已用 multi_match(strict=True) 实测过这条是
            #   榜单/官方歌单里的干净版本（HARD_BAD/SOFT_BAD 全毙、时长对得上），
            #   它标的 cv=1 比「网易 oct 查不到 = 未知」更可信 —— 保留，别抹成 0。
            #   抹成 0 的后果：端上 isCv() 会退回关键词兜底，干净标题虽不会误挂角标，
            #   但以后任何「按 cv 排序/筛选」的功能都会把这些歌归错档。
            stat["原唱"] += 1
        else:
            s["cv"] = 0
            stat["未知"] += 1
        # 名字带 live/伴奏/dj 等版本标记的，**降权但不删**（cv 保持，额外挂一个标记）
        if VER_RE.search(s.get("n") or ""):
            s["vm"] = 1
            stat["名字带版本标"] += 1
    log("[%s] 原唱标注：%d 首 ｜ %s"
        % (tag, len(arr), "、".join("%s %d" % (k, v) for k, v in stat.items() if v)))
    return arr, stat


def _fresh_bonus(pt, now_ms):
    """越新越加分，让「新歌」有资格顶掉等价热度的老歌"""
    if not pt:
        return 0.0
    yr = (now_ms - pt) / (365.25 * 24 * 3600 * 1000)
    if yr < 0:
        return 15.0          # 未来时间戳（预发行）按最新算
    if yr < 1:
        return 18.0
    if yr < 2:
        return 12.0
    if yr < 3:
        return 7.0
    if yr < 5:
        return 3.0
    if yr < 10:
        return 1.0
    return 0.0


def _norm(s):
    s = (s or "").lower()
    return "".join(ch for ch in s if ch not in " \t\r\n-_（）()[]【】·,.，。'\"!！?？~～&")


def _safe(t):
    """索引是「一行一条」的纯文本格式，原始字段里混进换行/行内分隔符会把行结构撑坏。

    ★ 实测血案（2026-10-06）：id=1317603367 的《Rigoletto: paraphrase de concert
      \\n  transcription by Franz Lizst》歌名里带 \\n，导致 idx-02.txt 凭空多出一行
      —— 索引 120360 行 vs 曲目 120359 首，客户端按行切分检索会错位。
      _norm() 会吃掉换行，但索引后两段要放**原名**给用户看，所以必须单独消毒。
    """
    return (t or "").replace("\n", " ").replace("\r", " ").replace(SEP, " ").strip()


_OCT_SEED = {}          # 从 _songs.jsonl 顺手捡到的 originCoverType（省一次网络请求）


def load_candidates():
    """读 _songs.jsonl（网易）+ _multi.jsonl（多源）→ 去重 + 剔除不可播。

    返回 {key: song}。key 的命名空间刻意分开（★ 2026-10-07）：
      · 网易曲目 → songId（int，与原逻辑完全一致）
      · 多源曲目 → "src:id"（如 "kw:475511182"）
    为什么必须分开：酷我 rid / 咪咕 contentId 都是**整数**，跟网易 songId 会撞号 ——
    一旦混进同一个 key 空间，"曲库 3 万首"里就会凭空少掉一批（互相覆盖）。
    """
    songs, drop = {}, {"字段不全": 0, "付费不可播": 0, "无版权下架": 0, "时长过短": 0,
                       "热度不足": 0, "多源重复": 0}
    nkey = {}                                    # 歌名|歌手 → 已在池中（跨源去重，网易优先）
    for line in open(SONGS_FILE, encoding="utf-8"):
        try:
            rec = json.loads(line)
        except Exception:
            continue
        for s in rec.get("songs") or []:
            sid = s.get("i")
            if "oct" in s:                       # 新版记录自带原唱标记 → 喂给原唱闸门
                _OCT_SEED[int(sid)] = [int(s.get("oct") or 0), int(s.get("ov") or 0)]
            if not sid or not s.get("n") or not s.get("a") or not s.get("p"):
                drop["字段不全"] += 1
                continue
            if s.get("f") in SKIP_FEE:
                drop["付费不可播"] += 1
                continue
            if s.get("nrc"):
                drop["无版权下架"] += 1
                continue
            if (s.get("d") or 0) < MIN_DUR:
                drop["时长过短"] += 1
                continue
            if (s.get("pop") or 0) < MIN_POP:
                drop["热度不足"] += 1
                continue
            if sid not in songs or (s.get("pop") or 0) > (songs[sid].get("pop") or 0):
                songs[sid] = s
                nkey[_skey(s.get("n"), s.get("a"))] = 1

    # ★ 多源池（QQ 榜单/分类歌单 + 酷狗榜 + 咪咕）：与网易池**同名同歌手直接丢弃**
    #   —— 网易是主库（有 originCoverType 官方原唱标记、音质最好），多源只做补充。
    nm = 0
    if os.path.exists(MULTI_FILE):
        for line in open(MULTI_FILE, encoding="utf-8"):
            try:
                s = json.loads(line)
            except Exception:
                continue
            sid = s.get("i")
            if not sid or not s.get("n") or not s.get("a"):
                drop["字段不全"] += 1
                continue
            k = _skey(s.get("n"), s.get("a"))
            if k in nkey:
                drop["多源重复"] += 1
                continue
            if (s.get("d") or 0) < MIN_DUR:
                drop["时长过短"] += 1
                continue
            nkey[k] = 1
            songs["%s:%s" % (s.get("src") or "wy", sid)] = s
            nm += 1
        if nm:
            log("多源池并入：%d 首（与网易池同名同歌手已去重 %d 首）" % (nm, drop["多源重复"]))
    return songs, drop


def apply_gate(songs):
    """质量闸门：歌手实力分档 + 垃圾黑名单。返回 (通过的歌, 实力歌手集合, 剔除明细)"""
    strong_cnt = {}
    for s in songs.values():
        if (s.get("pop") or 0) >= 30:
            strong_cnt[s["a"]] = strong_cnt.get(s["a"], 0) + 1
    strong = {a for a, c in strong_cnt.items() if c >= STRONG_N}
    out, drop2 = {}, {"翻唱伴奏黑名单": 0, "无名低热": 0, "时长超限": 0, "名字异常": 0}
    for sid, s in songs.items():
        t, a = s.get("n") or "", s.get("a") or ""
        if is_junk(t, a):
            drop2["翻唱伴奏黑名单"] += 1
            continue
        if len(t) > NAME_MAX or len(a) > ARTIST_MAX:
            drop2["名字异常"] += 1
            continue
        if not (MIN_DUR <= (s.get("d") or 0) <= MAX_DUR):
            drop2["时长超限"] += 1
            continue
        # ★ 多源曲目（src != wy）：来自榜单/官方分类歌单，且已过 strict 选曲 + 真音频验证。
        #   这些源根本不吐 pop（QQ/酷狗/咪咕 都没有热度字段），套网易的 pop 闸门会让整批
        #   被判「无名低热」全灭 —— 所以免检，但上面的垃圾/时长/名字三道闸门照样要过。
        if s.get("src") and s.get("src") != "wy":
            out[sid] = s
            continue
        floor = POP_EST if a in strong else POP_NEW
        if (s.get("pop") or 0) < floor:
            drop2["无名低热"] += 1
            continue
        out[sid] = s
    return out, strong, drop2


def cmd_stat():
    """只统计不打包：给 CI 判断「是否长够了新料」。结果同时写 GITHUB_OUTPUT"""
    songs, drop = load_candidates()
    kept, strong, drop2 = apply_gate(songs)
    man = {}
    try:
        man = json.load(open(os.path.join(CAT, "manifest.json"), encoding="utf-8"))
    except Exception:
        pass
    cur = man.get("count") or 0
    log("候选 %d ｜ 过闸门 %d 首 ｜ 线上现有 %d 首 ｜ 增量 %d"
        % (len(songs), len(kept), cur, len(kept) - cur))
    log("剔除：%s ｜ %s"
        % ("、".join("%s %d" % (k, v) for k, v in drop.items() if v),
           "、".join("%s %d" % (k, v) for k, v in drop2.items() if v)))
    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        with open(out, "a") as f:
            f.write("pass=%d\n" % len(kept))
            f.write("current=%d\n" % cur)
            f.write("grown=%d\n" % (len(kept) - cur))


def cmd_pack():
    songs, drop = load_candidates()
    log("候选唯一曲目 %d ｜ 已剔除：%s"
        % (len(songs), "、".join("%s %d" % (k, v) for k, v in drop.items() if v)))

    songs2, strong, drop2 = apply_gate(songs)
    log("质量闸门：%d → %d 首（实力歌手 %d 人；剔除：%s）"
        % (len(songs), len(songs2), len(strong),
           "、".join("%s %d" % (k, v) for k, v in drop2.items() if v)))
    songs = songs2

    # ★ 原唱/翻唱**标注**（放在排序截断之前：让原唱带着 cv=1 进入排序，
    #   截断时用 rank key 把原唱顶到前面，而不是先塞满翻唱再删掉）
    arr0, _og = original_gate(list(songs.values()), "pack")
    songs = {s["i"]: s for s in arr0}

    now_ms = int(time.time() * 1000)

    def _rank(x):
        """排序键：原唱 > 未知 > 翻唱；同档内再比热度+新鲜度。
        主人要求「原唱就是原唱 翻唱就是翻唱」—— 翻唱不是不要，是**排后面**。"""
        cv = x.get("cv") or 0
        pen = 0 if cv == 1 else (2 if cv == 0 else 6)     # 翻唱降 6 分
        if x.get("vm"):
            pen += 4                                       # 名字带 live/伴奏再降 4 分
        return -(((x.get("pop") or 0) + _fresh_bonus(x.get("pt"), now_ms)) - pen * 8)

    arr = sorted(songs.values(), key=_rank)
    if KEEP and len(arr) > KEEP:
        cut = (arr[KEEP - 1].get("pop") or 0)
        arr = arr[:KEEP]
        log("★ 质量截断：%d → %d 首（第 %d 名热度 %d）" % (len(songs), len(arr), KEEP, cut))

    pops = [x.get("pop") or 0 for x in arr]
    if pops:
        pops_sorted = sorted(pops)
        q = lambda p: pops_sorted[min(len(pops_sorted) - 1, int(len(pops_sorted) * p))]
        log("入库 %d 首 ｜ 热度分位 p10=%d p50=%d p90=%d ｜ 近一年新歌 %d 首"
            % (len(arr), q(0.10), q(0.50), q(0.90),
               sum(1 for x in arr if (now_ms - (x.get("pt") or 0)) < 365.25 * 24 * 3600 * 1000)))
        band = lambda lo, hi: sum(1 for p in pops if lo <= p < hi)
        log("分层：热门(pop≥70) %d ｜ 40-69 %d ｜ 25-39 %d ｜ 10-24 %d"
            % (band(70, 101), band(40, 70), band(25, 40), band(10, 25)))
        log("歌手 %d 位 ｜ 曲库容量 %.1f MB(预估)"
            % (len({x["a"] for x in arr}), len(arr) * 215 / 1048576))

    # ★ 音频级可播闸门（放在截断之后：只对真正要发布的集合发 HEAD，省流量）
    arr, _av = audio_gate(arr, "pack")

    # ★★ 2026-10-08：硬过滤缺 i/n/a 的曲目。
    #   体检（catalog_verify.py）把 i(id)/n(名)/a(歌手) 列为必需，缺一条就阻断**整批**发布；
    #   这三项端上也是非有不可（id 决定能不能播、名/歌手决定显示与搜索索引）。
    #   血案：run 37736589880 里 79,457 首中有 29 条缺字段（0.04%），
    #   结果把 7.9 万首的整批发布全盘卡死，线上停在旧的 68,903 首。
    #   与其因瑕疵全盘陪葬，不如在这里就剔掉——代价 0.04%，换来闸门恒过。
    _n0 = len(arr)
    arr = [x for x in arr if all(x.get(k) for k in ("i", "n", "a"))]
    if len(arr) != _n0:
        log("★ 必需字段过滤：%d → %d 首（剔 %d 条缺 i/n/a）" % (_n0, len(arr), _n0 - len(arr)))

    _write_catalog(arr)

    # ★ 2026-10-07 新增：同步刷新「包内曲库分块」（cat-manifest.js / cat-idx-NN.js / cat-sh-NN.js）。
    #   血案背景：mkcatalog_js.py 以前**没有任何流水线调用** → data/catalog/js/ 长期停在
    #   上一代（实测 101,256 首、未过质量闸门）而 manifest.json 已是 31,681 首（已洗过）。
    #   两者不一致的后果：App 优先读**包内**清单 → 装机后搜到的是没洗过的旧库
    #   （翻唱/伴奏/有声书全在），只有 CDN 可达时 refresh() 才会换成新库；
    #   而中国网络直连 jsdelivr 常常不可达 → 就永远停在旧库上。
    _sync_js()


def _sync_js():
    """按刚写好的 manifest.json 重新生成包内 JS 分块。

    放在 cmd_pack() 里（而不是 CI 的独立步骤）是为了**原子性**：
    pack 写完 manifest.json 就立刻生成 js，永远不会出现「清单已换、分块还是上一代」。
    失败只告警不抛出 —— 曲库本体已经发布成功，不该因为分块生成把整轮采集成果废掉。
    """
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import mkcatalog_js
        mkcatalog_js.main()
        log("包内曲库分块已刷新 → data/catalog/js/")
    except Exception as e:
        log("⚠ 包内曲库分块刷新失败（不影响曲库发布本身）：%r" % (e,))


def _sid(v):
    """JSON 安全的歌曲 id：**短的纯数字**写数字，其余一律写字符串。

    ★ 2026-10-07 实测血案（多源上线当天抓到）：
      咪咕 contentId 是 18 位整数，如 600930000002751847 —— 远超 JS 的
      `Number.MAX_SAFE_INTEGER`（2^53-1 = 9007199254740991）。
      一旦以 JSON 数字形式落到分片里，JS 侧 JSON.parse 直接把它变成
      `600930000002751900`（尾部 3 位被抹平）→ 端上拿错 id 请求咪咕 →
      **多源曲目 100% 播不出声**，而且不报错、静默失败，最难查。
      Python 的 int 无上限、JS 的 Number 有 —— 只能由**写端让路**。
      同理 B 站 bvid 是 "BV1xx…" 这类字母串，`+"BV1xx"` = NaN，会被 `if(!id)` 静默丢掉。

    判据：≤15 位纯数字 → int（网易 songId / 酷我 rid 都落在这一档，保持与老数据同型）；
          其余（18 位咪咕 id、bvid、带字母的）→ str，端上按**不透明字符串**处理。
    """
    if isinstance(v, int):
        return v if -9007199254740991 <= v <= 9007199254740991 else str(v)
    s = str(v or "")
    return int(s) if s.isdigit() and len(s) <= 15 else s


def _write_catalog(arr):
    """把最终曲目列表写成 分片 + 索引 + manifest（pack / verify 共用）"""
    os.makedirs(CAT, exist_ok=True)
    for old in os.listdir(CAT):
        if old.startswith("shard-") or old.startswith("idx-") or old in ("manifest.json", "search.json"):
            os.remove(os.path.join(CAT, old))

    # ★ 先把 id 统一成「JSON 安全型」，再同时喂给分片与索引 —— 两处必须同型，
    #   否则端上 `refs.set(s.id)` / `refs.get(rec.i)` 一个字符串一个数字，封面永远补不上。
    for x in arr:
        x["i"] = _sid(x.get("i"))
        if x.get("ov"):
            x["ov"] = _sid(x.get("ov"))

    shards = []
    for i in range(0, len(arr), SHARD):
        chunk = arr[i:i + SHARD]
        name = "shard-%04d.json" % (i // SHARD)
        with open(os.path.join(CAT, name), "w", encoding="utf-8") as fh:
            json.dump(chunk, fh, ensure_ascii=False, separators=(",", ":"))
        shards.append({"file": name, "count": len(chunk)})

    # ── 搜索索引：紧凑纯文本分块 ───────────────────────────────────────────
    # 为什么不用 JSON 数组：30 万+ 条 [[名,歌手,id,片],…] 解析后对象开销约是文本体积的 10 倍，
    # WKWebView 会被拖垮、甚至被 jetsam 杀掉。改成一「行」一条、\u0001 分隔的纯文本块后：
    #   · 手机端只保存原始字符串（无 JSON.parse、无逐条对象）→ 内存 ≈ 文本体积；
    #   · 检索用 indexOf 直接在字符串上滑，命中才切那一行 → 零额外分配。
    # 行格式：归一化歌名 \u0001 归一化歌手 \u0001 歌曲id \u0001 片号 \u0001 原名 \u0001 原歌手
    #   \u0001 原唱标记(cv) \u0001 原唱id(ov) \u0001 播放源(src)
    #   —— 前两段用内核同款 norm() 规则（小写+去空白/标点），用户输入什么都能搜到；
    #      后两段只用于「显示」，因为 norm 会吃掉空格与标点，不能拿它当标题给用户看。
    #      cv：1=原唱 / 2=翻唱 / 0=未知（客户端据此置顶原唱、给翻唱挂角标，见 kernel isCv）
    #      ov：翻唱指向的原唱 songId（0/空 = 无）→ 客户端可显示「原唱：XXX」
    #      src：★ 2026-10-07 多源之后新增，wy/kw/migu/bili —— 决定客户端怎么拼直链
    #        （wy→outer/url；kw→antiserver；migu→listenV2）。老数据只有 8 段，
    #        客户端把缺省当 "wy" 处理，向后兼容。
    lines = []
    si = 0
    for n, s in enumerate(arr):
        if n and n % SHARD == 0:
            si += 1
        lines.append(SEP.join((_norm(s["n"]), _norm(s["a"]), str(s["i"]), str(si),
                               _safe(s["n"]), _safe(s["a"]),
                               str(s.get("cv") or 0), str(s.get("ov") or 0),
                               s.get("src") or "wy")))

    idx_files = []
    for i in range(0, len(lines), IDX_CHUNK):
        name = "idx-%02d.txt" % (i // IDX_CHUNK)
        # ★ 2026-10-07 血案：必须显式 newline="\n"。
        #   text 模式默认 newline=None → Windows 上把 "\n" 翻成 "\r\n"，
        #   于是每行**最后一个字段**（当前是 src）变成 "migu\r"，
        #   端上 `sid==="migu"` 恒为 false → 又多源静默退回网易直链（无报错、最难查）。
        #   Linux runner 不会重现，只在 Windows 本机产物上踩——所以这里从根上钉死换行符。
        with open(os.path.join(CAT, name), "w", encoding="utf-8", newline="\n") as fh:
            fh.write("\n".join(lines[i:i + IDX_CHUNK]))
        idx_files.append(name)

    idx_bytes = sum(os.path.getsize(os.path.join(CAT, f)) for f in idx_files)
    shard_bytes = sum(os.path.getsize(os.path.join(CAT, s["file"])) for s in shards)
    man = {"updated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
           "count": len(arr), "shardSize": SHARD, "shards": shards,
           "idx": {"chunks": idx_files, "chunkSize": IDX_CHUNK, "total": len(lines),
                   "sep": SEP,
                   "fields": ["nameN", "artistN", "id", "shard", "name", "artist", "cv", "ov", "src"]},
           "schema": {"i": "songId", "n": "name", "a": "artist", "b": "album",
                      "p": "cover", "d": "duration_ms", "f": "fee",
                      "pop": "popularity0-100", "pt": "publishTime_ms",
                      "cv": "1=原唱/2=翻唱/0=未知", "ov": "翻唱指向的原唱id",
                      "src": "播放源 wy|kw|migu|bili"},
           "ranked": "pop + freshness",
           # ★ 2026-10-07：不再是写死的 ["netease"]，而是按实际入库来源统计
           #   （主人：「为什么就死盯着网易呢！全网什么叫全网？」）
           "sources": sorted({(s.get("src") or "wy") for s in arr})}
    json.dump(man, open(os.path.join(CAT, "manifest.json"), "w", encoding="utf-8", newline="\n"),
              ensure_ascii=False, indent=1)
    log("分片完成：%d 片 / %d 首 / %.1f MB" % (len(shards), len(arr), shard_bytes / 1048576))
    log("索引完成：%d 块 / %d 行 / %.1f MB（每块 ~%.2f MB）"
        % (len(idx_files), len(lines), idx_bytes / 1048576,
           (idx_bytes / max(1, len(idx_files))) / 1048576))
    log("总体：曲库 %.1f MB + 索引 %.1f MB = %.1f MB"
        % (shard_bytes / 1048576, idx_bytes / 1048576, (shard_bytes + idx_bytes) / 1048576))


# =============================================================== 多源采集（QQ / 酷狗）
def _skey(t, s):
    """与端上 skey() / build._skey() 逐字符对齐：归一化后按**码点** 40/30 截断。"""
    return "%s|%s" % (_norm(t)[:40], _norm(s)[:30])


def _ad():
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import adapters as A
    return A


def _existing_keys():
    """网易池已采元数据的 歌名|歌手 键集（避免多源重复采同一首）"""
    keys = set()
    if not os.path.exists(SONGS_FILE):
        return keys
    for line in open(SONGS_FILE, encoding="utf-8"):
        try:
            rec = json.loads(line)
        except Exception:
            continue
        for s in rec.get("songs") or []:
            if s.get("n") and s.get("a"):
                keys.add(_skey(s["n"], s["a"]))
    return keys


def _multi_keys():
    """_multi.jsonl 里已采过的键集（断点续跑用）"""
    keys = set()
    if not os.path.exists(MULTI_FILE):
        return keys
    for line in open(MULTI_FILE, encoding="utf-8"):
        try:
            r = json.loads(line)
        except Exception:
            continue
        if r.get("n") and r.get("a"):
            keys.add(_skey(r["n"], r["a"]))
    return keys


def _multi_tried():
    """「最近试过」的键集（含失败），带冷却期 —— 见 MULTI_TRIED 处的说明。

    文件是 append-only 的 jsonl（同键会重复出现），只取每条键的**最后一次**时间戳；
    超过 MULTI_RETRY_DAYS 的失败允许重试（说不定后来某源上架了）。
    只保留最近 40 万条以约束内存（远大于任何现实池子）。
    """
    last = {}
    if os.path.exists(MULTI_TRIED):
        try:
            lines = open(MULTI_TRIED, encoding="utf-8").read().splitlines()[-400000:]
        except Exception:
            lines = []
        for line in lines:
            try:
                r = json.loads(line)
            except Exception:
                continue
            k, t = r.get("k"), r.get("t") or 0
            if k and t >= last.get(k, 0):
                last[k] = t
    cutoff = time.time() - MULTI_RETRY_DAYS * 86400
    return {k for k, t in last.items() if t >= cutoff}


def _kg_ranks(A, limit):
    """酷狗榜单（rank/list → rank/song）。

    字段实测（2026-10-07）：歌手藏在 `authors[].author_name`（不是 singername，那个是 None），
    封面是 `album_sizable_cover` 的 {size} 模板，时长 duration 是**秒**。
    """
    out = []
    r = A.http("http://mobilecdn.kugou.com/api/v3/rank/list?json=true&page=1&pagesize=60",
               {"Referer": "https://www.kugou.com/"})
    info = ((A.jload(r) or {}).get("data") or {}).get("info") or []
    for x in info[:limit]:
        rid = x.get("rankid")
        if not rid:
            continue
        try:
            r2 = A.http("http://mobilecdn.kugou.com/api/v3/rank/song?version=9108&rankid=%s"
                        "&page=1&pagesize=%d&withsong=1" % (rid, MULTI_KG_PER_RANK),
                        {"Referer": "https://www.kugou.com/"})
        except Exception:
            continue
        songs = ((A.jload(r2) or {}).get("data") or {}).get("info") or []
        rows = []
        for y in songs:
            au = y.get("authors") or []
            sing = "/".join(z.get("author_name", "") for z in au if isinstance(z, dict))
            cov = ((y.get("album_sizable_cover") or "")
                   .replace("{size}", "480").replace("http://", "https://"))
            rows.append({"title": y.get("songname") or "", "singer": sing,
                         "duration": A.dur_s(y.get("duration")), "cover": cov})
        out.append({"name": x.get("rankname") or rid, "songs": rows})
    return out


def _collect_tasks(A):
    """汇总「待补曲目」：[(title, singer, dur_s, cover, album, 来源)]"""
    tasks, seen = [], set()

    def add(t, s, d, cov, alb, frm):
        # ★ 2026-10-07 血案：QQ/酷我 返回的歌手串里 `&` 是**字面量** `\u0026`（8 个字符）。
        #   不还原的话它同时污染两处：① 拿这串去各源搜 → 搜出无关结果，命中率白白掉；
        #   ② 直接入库 → App 上显示 "周杰伦\u0026五月天"。这里统一还原（幂等）。
        t, s = A._unesc(t).strip(), A._unesc(s).strip()
        if not t or not s:
            return
        k = _skey(t, s)
        if k in seen:
            return
        seen.add(k)
        tasks.append((t, s, A.dur_s(d), cov or "", alb or "", frm))

    # ① QQ 八大榜单（实测 100 条/榜、747ms，且**第一条就是原唱**）
    for topid, name, lim in A.QQ_TOPLISTS:
        if len(tasks) >= MULTI_POOL:
            break
        try:
            r = A.qq_toplist(topid, name, min(lim, MULTI_QQ_PER_RANK))
            rows = r.get("songs") or []
            for x in rows:
                add(x.get("title"), x.get("singer"), x.get("duration"),
                    x.get("cover"), x.get("album"), "QQ·%s" % name)
            log("  QQ %-8s %d 首" % (name, len(rows)))
        except Exception as e:
            log("  ! QQ %s 失败：%s" % (name, e))

    # ② QQ 分类歌单（多语种分组 × 64 分类 × **多种排序**，实测可用）
    #    ★ 2026-10-07 扩容：原来只取 sort=3、每分类 4 个歌单 → 全池仅 ~1 万条候选；
    #      而 CI 每轮能试 8000 条、命中 ~39%，**两三轮就把池子抽干**，之后采集量断崖。
    #      现在 sort 维度可配（1/2/3/5 是不同榜，歌单集合几乎不重叠），
    #      并把「取歌单内歌曲」并发化 —— 几千个歌单串行要跑十几分钟，并发后几十秒。
    try:
        tags = A.qq_diss_tags()
        sorts = [int(z) for z in str(MULTI_QQ_SORTS).split(",") if z.strip().isdigit()]
        # 歌单数上限：够取满池子即可（热门歌单之间会大量重歌，留 2 倍余量）
        pl_cap = max(200, (MULTI_POOL // max(MULTI_SONGS_PER_PL, 1)) * 2)
        pairs, ncat = [], 0
        for g in tags:
            if len(pairs) >= pl_cap:
                break
            for c in (g.get("cats") or []):
                if len(pairs) >= pl_cap:
                    break
                for st in sorts:
                    if len(pairs) >= pl_cap:
                        break
                    try:
                        pls = A.qq_diss_list(c.get("id"), sort=st, n=MULTI_PL_PER_CAT)
                    except Exception:
                        continue
                    ncat += 1
                    for p in pls:
                        if p.get("dissid"):
                            pairs.append((g.get("group"), str(p["dissid"])))
        log("  QQ 歌单：%d 个「分类×排序」组合 → %d 个歌单待取（并发 %d）"
            % (ncat, len(pairs), MULTI_WORKERS))

        npl = 0
        if pairs:
            def _pl(d):
                try:
                    return A.qq_diss_songs(d, MULTI_SONGS_PER_PL)
                except Exception:
                    return []
            with ThreadPoolExecutor(max_workers=MULTI_WORKERS) as ex:
                for (grp, _d), ss in zip(pairs, ex.map(_pl, [p[1] for p in pairs])):
                    if len(tasks) >= MULTI_POOL:
                        break
                    if not ss:
                        continue
                    npl += 1
                    for x in ss:
                        add(x.get("title"), x.get("singer"), x.get("duration"),
                            x.get("cover"), x.get("album"), "QQ·%s" % grp)
        log("  QQ 歌单 %d 个 → 累计 %d 条" % (npl, len(tasks)))
    except Exception as e:
        log("  ! QQ 歌单分类失败：%s" % e)

    # ③ 酷狗榜单（实测 55 个榜、每榜 50 首）
    try:
        ranks = _kg_ranks(A, MULTI_KG_RANKS)
        for r in ranks:
            for x in r["songs"]:
                add(x["title"], x["singer"], x["duration"], x["cover"], "", "KG·%s" % r["name"])
        log("  酷狗榜单 %d 个 → 累计 %d 条" % (len(ranks), len(tasks)))
    except Exception as e:
        log("  ! 酷狗榜单失败：%s" % e)

    return tasks


_SEEN_EXC = set()


def _log_exc(tag):
    """同类未捕获异常只打一次完整栈（12 并发下不然会刷屏），但一定要打。"""
    if tag in _SEEN_EXC:
        return
    _SEEN_EXC.add(tag)
    try:
        log("  ! 多源未捕获异常（首次出现，完整栈）—— 这条线索必须查，不能吞：")
        log(traceback.format_exc())
    except Exception:
        pass


def _why_none(tr):
    """把 multi_match 的 trace 归成**一个可直接统计的短标签**。

    为什么要它：`_multi_one` 原先失败就静默 return None，实战里「30 试只中 4 条」
    完全查不出死在哪（连 strict=False 都失败 13/15 且无任何异常）。有了这个标签，
    日志里就能直接看到「主因分布」，一眼定位是哪道闸门/哪个源在吞量。

    优先级 noplay > 闸门 > 异常 > 无候选：
      noplay 最值钱 —— 说明**已经找到干净候选了**，只是直链拿不到（VIP/无链），
      这类是「源能力问题」，和「闸门判错」是两回事，必须分开看。
    """
    tr = tr or {}
    if tr.get("noplay"):
        return "不可播:" + ",".join(tr["noplay"])
    rej = tr.get("rej") or {}
    if rej:
        k, n = max(rej.items(), key=lambda x: x[1])
        return "闸门x%d:%s" % (n, k)
    if tr.get("err"):
        return "异常:" + str(tr["err"][0])[:40]
    if tr.get("nocand"):
        return "无候选:" + ",".join(tr["nocand"])
    return "未知"


def _multi_one(A, task):
    """一条候选 → multi_match(strict) 找能播源 → 落库记录。

    返回 (rec, why, tr)：
      rec = 落库记录 或 None
      why = 失败时**单一可指认原因**（见 _why_none；成功时为 "OK"）
      tr  = multi_match 的逐源去向明细（用于全量汇总，不落盘）
    """
    t, s, d, cov, alb, frm = task
    tr = {}
    try:
        m = A.multi_match(t, s, dur_ms=int(d or 0) * 1000, strict=True, trace=tr)
    except Exception as e:
        # ★ 2026-10-07 血案：原先这里只回 `type(e).__name__`，把 message 整个丢掉 ——
        #   于是 multi_match 里一个 `not res_ok`（变量名已被删）的 NameError 被静默吞掉
        #   17/300 条候选（≈失败量的 19%），而且它抛在 `for sid in order` 循环里，
        #   连累后面还没试的源全都没跑。现在带上 message + 打一次完整栈。
        _log_exc(type(e).__name__)
        return None, "异常:%s(%s)" % (type(e).__name__, str(e)[:60]), tr
    if not m:
        return None, _why_none(tr), tr
    if not m.get("id"):
        return None, "无长效id", tr
    dur = int(d or 0) or int(m.get("dur") or 0)
    rec = {"i": m["id"], "src": m["src"], "n": t, "a": s,
           "b": (alb or m.get("album") or "")[:60],
           "p": cov or m.get("cover") or "",
           "d": dur * 1000, "f": 0,
           # 多源曲目来自**榜单/官方分类歌单**，本身就是热度背书；网易那套 pop 闸门
           # 对它们不适用（这些源根本不吐 pop），故给满值并在 apply_gate 里免检。
           "pop": 100, "pt": 0, "nrc": 0,
           # strict 模式已把翻唱/Live/DJ/伴奏全毙 → 存活即视为原唱
           "cv": 1, "ov": 0, "from": frm}
    return rec, "OK", tr


def cmd_multi():
    """★ 多源扩充：把 QQ 榜单/分类歌单 + 酷狗榜单 的曲目并进曲库池。

    每条候选都跑 multi_match(strict=True) 拿**实测能播**的长效 id：
      · 命中 wy   → 用网易 songId（端上零改动，走原有 outer/url 直链）
      · 命中 kw   → 用酷我 rid + src="kw"（端上按 antiserver 拼直链）
      · 命中 migu → 用咪咕 contentId + src="migu"
      · 全源都只有脏版本 → 丢弃（主人：「宁可不治也不能错治」）

    产出 data/catalog/_multi.jsonl（append-only，可断点续跑）。
    """
    if not MULTI_ON:
        log("多源扩充：已关闭（HV_MULTI=0）")
        return
    A = _ad()
    have, done, tried = _existing_keys(), _multi_keys(), _multi_tried()
    log("多源扩充：网易池键 %d 个 ｜ _multi 已采 %d 条 ｜ 冷却中（%d 天内试过）%d 条"
        % (len(have), len(done), MULTI_RETRY_DAYS, len(tried)))

    tasks = _collect_tasks(A)
    log("多源候选：%d 条（源内已按 歌名|歌手 去重）" % len(tasks))
    # ★ 跳过集必须含「试过的（含失败）」—— 否则失败候选永远占着队首，池尾轮不到。
    skip = have | done | tried
    todo = [t for t in tasks if _skey(t[0], t[1]) not in skip]
    log("待补 %d 条（与网易池/已采/冷却中重合 %d 条）" % (len(todo), len(tasks) - len(todo)))
    # ★ 2026-10-07：**轮转顺序**，别按收集顺序取前 N。
    #   实测血案：候选是「QQ 八大榜单 → QQ 分类歌单 → 酷狗榜」的顺序拼的，而 QQ 榜单
    #   全是主流歌 —— 它们**几乎全在网易池里**（218877 键）。skip 之后留在队首的，
    #   恰恰是「各大源都没有干净原唱」的冷门版本，最难命中。于是每轮限额预算全砸在
    #   这批硬骨头上（实测连跑两轮 40 条，命中 0）。
    #   改成随时间变化的洗牌，每轮在全池均匀取样；配合 _multi_tried 冷却，
    #   整个池子能被均匀、快速地覆盖一遍。
    random.Random(int(time.time()) // 60).shuffle(todo)
    if MULTI_MAX and len(todo) > MULTI_MAX:
        todo = todo[:MULTI_MAX]
        log("  → 本轮限量 %d 条（HV_MULTI_MAX）" % MULTI_MAX)
    if not todo:
        log("多源扩充：无新候选，跳过")
        return

    t0, ok, srcs = time.time(), 0, {}
    why, rej_tot, noplay_tot = {}, {}, {}
    tried_keys, wy_dead = [], 0
    os.makedirs(CAT, exist_ok=True)
    now = int(time.time())

    def _run(tk):
        """把「键」随任务一起带回来 —— 不要去猜 ex.map 的下标。"""
        return _skey(tk[0], tk[1]), _multi_one(A, tk)

    with ThreadPoolExecutor(MULTI_WORKERS) as ex, open(MULTI_FILE, "a", encoding="utf-8") as fh:
        for i, (k, (rec, w, tr)) in enumerate(ex.map(_run, todo), 1):
            tried_keys.append(k)
            # 健康度：网易这路整轮「搜索无候选」= 大概率被风控/接口异常（见收尾的自检）
            if "wy" in (tr.get("nocand") or []):
                wy_dead += 1
            if rec:
                ok += 1
                srcs[rec["src"]] = srcs.get(rec["src"], 0) + 1
                fh.write(json.dumps(rec, ensure_ascii=False, separators=(",", ":")) + "\n")
            else:
                why[w] = why.get(w, 0) + 1
                for k2, n in (tr.get("rej") or {}).items():
                    rej_tot[k2] = rej_tot.get(k2, 0) + n
                for s_ in (tr.get("noplay") or []):
                    noplay_tot[s_] = noplay_tot.get(s_, 0) + 1
            if i % 200 == 0:
                fh.flush()
                top = sorted(why.items(), key=lambda x: -x[1])[:3]
                log("  … %d/%d（命中 %d ｜ %s ｜ 未中主因 %s ｜ 网易无候选 %d ｜ %.0fs）"
                    % (i, len(todo), ok, srcs, top, wy_dead, time.time() - t0))
    log("多源扩充完成：新增 %d 条 ｜ 源分布 %s ｜ 耗时 %.0fs"
        % (ok, srcs, time.time() - t0))

    # ★ 2026-10-07：写「已尝试」台账 **必须带健康度自检**。
    #   为什么：若某个源被风控（整轮请求都返回空），所有候选都会被判「没找到」，
    #   一旦照写台账，**整池候选会被白白冷却 14 天**（等风控恢复后也不重试）——
    #   台账反而成了毒药。这里用「网易这路无候选的比例」当场检：
    #   正常情况该比例远低于 90%（实测 ~10-20%），一旦 ≥90% 就认定本轮不可信。
    cred = True
    if len(todo) >= 30 and wy_dead / float(len(todo)) > 0.9:
        cred = False
        log("  ⚠️ 本轮 %d/%d 的候选在网易这路「搜索无候选」，疑似风控/接口异常 —— "
            "**不写「已尝试」台账**，避免把整池候选白白冷却 %d 天"
            % (wy_dead, len(todo), MULTI_RETRY_DAYS))
    if cred and tried_keys:
        os.makedirs(CAT, exist_ok=True)
        with open(MULTI_TRIED, "a", encoding="utf-8") as ftr:
            for k in tried_keys:
                ftr.write('{"k":%s,"t":%d}\n' % (json.dumps(k, ensure_ascii=False), now))
        log("  已记账 %d 条（%d 天内不再重试失败项）" % (len(tried_keys), MULTI_RETRY_DAYS))
    # ★ 2026-10-07 新增：把「未命中」摊开成可读明细 —— 原先这里只有一行「新增 N 条」，
    #   掉量的原因全黑，导致「命中率低」根本没法定位（详见 _why_none 的说明）。
    if why:
        log("  ↳ 未命中 %d 条 ｜ 主因分布：" % sum(why.values()))
        for k, n in sorted(why.items(), key=lambda x: -x[1])[:10]:
            log("      %5d  %s" % (n, k))
    if rej_tot:
        log("  ↳ 闸门拒绝明细（全源累计，仅用于诊断，不落库）：")
        for k, n in sorted(rej_tot.items(), key=lambda x: -x[1])[:12]:
            log("      %5d  %s" % (n, k))
    if noplay_tot:
        log("  ↳ 「已找到干净候选但直链拿不到」的源（VIP/无链，属源能力问题）：%s" % noplay_tot)


def meta_gate(arr, tag="verify"):
    """对**已有**曲目列表套用「元数据质量闸门」（与 pack 同款标准）。

    为什么必须补这一步（实测证据 2026-10-06）：
      catalog_verify.py 会硬卡「p50 热度 ≥10、垃圾命名 0 命中、时长 30s~15min、
      无 VIP 付费、无版权下架」，而 cmd_verify 原先只做「原唱 + 音频」双闸。
      结果洗完的库照样过不了自家体检：13 万 → 12 万，p50 仍是 5、垃圾命名仍 2.87 万条
      —— 等于「洗了个寂寞」。把同款标准搬进来，wash 的产物才真正可直接发布。
    """
    drop = {}

    def _apply(a, name, ok):
        keep = [x for x in a if ok(x)]
        if len(keep) != len(a):
            drop[name] = len(a) - len(keep)
        return keep

    a = _apply(arr, "付费不可播", lambda x: x.get("f") not in SKIP_FEE)
    a = _apply(a, "无版权下架", lambda x: not x.get("nrc"))
    a = _apply(a, "时长越界", lambda x: MIN_DUR <= (x.get("d") or 0) <= MAX_DUR)
    a = _apply(a, "热度不足", lambda x: (x.get("pop") or 0) >= MIN_POP)
    a = _apply(a, "垃圾命名", lambda x: not is_junk(x.get("n"), x.get("a")))
    log("[%s] 元数据闸门：%d → %d 首 ｜ 剔除 %s"
        % (tag, len(arr), len(a),
           "、".join("%s %d" % (k, v) for k, v in drop.items()) or "无"))
    return a


def cmd_verify():
    """对**现有** catalog 做「元数据 + 原唱 + 音频可播」三重体检并原地重写。

    为什么要独立一条：线上 13 万曲库是按元数据闸门打包的（fee 可用性靠猜），
    实测混进了三类不该有的东西：
      · 「标称免费、实际 VIP」的死链与试听片段（周杰伦 33 首里 28 首）；
      · **别人的翻唱**（点名歌手 385 首里只有 94 首是原版录音室版）；
      · live/综艺版 —— 歌词时间轴与原唱版对不上，主人听到的就是「词不同步不对版」。
    本命令不重新采集，只对已发布的分片逐首复核，剔除不合格项后重写分片/索引/manifest
    —— 一次把存量洗一遍。
    """
    files = sorted(f for f in os.listdir(CAT) if f.startswith("shard-") and f.endswith(".json"))
    if not files:
        log("× 没有找到 shard-*.json，先跑 pack")
        return
    arr = []
    for f in files:
        with open(os.path.join(CAT, f), encoding="utf-8") as fh:
            d = json.load(fh)
        arr.extend(d if isinstance(d, list) else (d.get("songs") or []))
    log("现有曲库 %d 首（%d 片）→ 开始三重体检" % (len(arr), len(files)))
    arr = meta_gate(arr, "verify")
    arr, _og = original_gate(arr, "verify")        # ★ 只标注，不再删翻唱
    keep, _ = audio_gate(arr, "verify")
    now_ms = int(time.time() * 1000)

    def _rank(x):
        cv = x.get("cv") or 0
        pen = 0 if cv == 1 else (2 if cv == 0 else 6)
        if x.get("vm"):
            pen += 4
        return -(((x.get("pop") or 0) + _fresh_bonus(x.get("pt"), now_ms)) - pen * 8)

    keep.sort(key=_rank)
    log("重写曲库：%d → %d 首" % (len(arr), len(keep)))
    _write_catalog(keep)


def cmd_wash():
    """verify 的别名：把存量曲库按「原唱 + 可播」重洗一遍（本地手动触发用）"""
    cmd_verify()


if __name__ == "__main__":
    cmd = (sys.argv[1] if len(sys.argv) > 1 else "enum").lower()
    {"enum": cmd_enum, "ids": cmd_ids, "artists": cmd_artists,
     "songs": cmd_songs, "pack": cmd_pack, "stat": cmd_stat,
     "verify": cmd_verify, "wash": cmd_wash, "multi": cmd_multi}[cmd]()
