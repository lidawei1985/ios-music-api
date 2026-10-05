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
import json, os, sys, time, ssl, re, urllib.request, urllib.parse, random
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
MIN_PL_TRACKS = int(os.environ.get("HV_MIN_TRACKS") or "50")
MAX_PLAYLISTS = int(os.environ.get("HV_MAX_PLAYLISTS") or "9000")
TARGET_IDS = int(os.environ.get("HV_TARGET_IDS") or "260000")


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
    log("分类数 =", len(subs))

    jobs = [(sub, order, off) for sub in subs for order in ("hot", "new")
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

    pls.sort(key=lambda x: (-x["n"], -x["play"]))
    json.dump({"updated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
               "count": len(pls), "playlists": pls},
              open(PL_FILE, "w", encoding="utf-8"), ensure_ascii=False)
    log("歌单池 %d 个，曲目理论上限 %d → %.0fs" % (len(pls), sum(p["n"] for p in pls), time.time() - t0))


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
                    "nrc": 1 if t.get("noCopyrightRcmd") else 0})
    return out, aids, None


def cmd_songs():
    store = json.load(open(IDS_FILE, encoding="utf-8"))
    pool = list(store.get("playlists", {}).values()) + list(store.get("artists", {}).values())
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
    aids = json.load(open(ART_FILE, encoding="utf-8"))
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
KEEP = int(os.environ.get("HV_KEEP") or "250000")   # 只保留评分最高的 N 首
MIN_POP = int(os.environ.get("HV_MIN_POP") or "0")  # 热度硬地板（0 = 只按相对排名截断）
MIN_DUR = 30000         # 30 秒以下多为过场/语音，不是歌
MAX_DUR = int(os.environ.get("HV_MAX_DUR") or "900000")   # 15 分钟以上多为组曲/有声书
NAME_MAX = 80           # 歌名超长的多是「曲目+选段+指挥+乐团」全堆一块的灌水
ARTIST_MAX = 60

# ===== 质量闸门（2026-10-05 真机血案：31% 翻唱垃圾混进曲库，主人怒斥"不会唱歌的也进来了"）=====
# 翻唱/伴奏/DJ 版/铃声/助眠白噪音/清唱哼唱……一律不要；
# 实力歌手（候选池里有 >= STRONG_N 首 pop>=30 的歌）全碟收录（pop>=POP_EST），
# 陌生歌手必须 pop>=POP_NEW（爆款才给进门）——宁缺毋滥。
JUNK_RE = re.compile(
    r"翻唱|cover|伴奏|instrumental|dj版|抖音|铃声|纯音乐|钢琴版|吉他版|尤克里里|八音盒|"
    r"口琴|陶笛|葫芦丝|萨克斯|二胡|古筝|电子琴|哼唱|清唱|翻自|ktv|慢速|加速|降调|升调|"
    r"remix|八轨|和声版|消音|立体声环绕|睡眠|白噪音|胎教|助眠|asmr|钢琴曲|轻音乐", re.I)
STRONG_N = int(os.environ.get("HV_STRONG_N") or "5")
POP_EST = int(os.environ.get("HV_POP_EST") or "10")
POP_NEW = int(os.environ.get("HV_POP_NEW") or "40")


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


def cmd_pack():
    songs, drop = {}, {"字段不全": 0, "付费不可播": 0, "无版权下架": 0, "时长过短": 0, "热度不足": 0}
    for line in open(SONGS_FILE, encoding="utf-8"):
        try:
            rec = json.loads(line)
        except Exception:
            continue
        for s in rec.get("songs") or []:
            sid = s.get("i")
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
    log("候选唯一曲目 %d ｜ 已剔除：%s"
        % (len(songs), "、".join("%s %d" % (k, v) for k, v in drop.items() if v)))

    # ---- 质量闸门：歌手实力分档 + 翻唱/伴奏黑名单 ----
    strong_cnt = {}
    for s in songs.values():
        if (s.get("pop") or 0) >= 30:
            strong_cnt[s["a"]] = strong_cnt.get(s["a"], 0) + 1
    strong = {a for a, c in strong_cnt.items() if c >= STRONG_N}
    songs2, drop2 = {}, {"翻唱伴奏黑名单": 0, "无名低热": 0, "时长超限": 0, "名字异常": 0}
    for sid, s in songs.items():
        t, a = s.get("n") or "", s.get("a") or ""
        if JUNK_RE.search(t) or JUNK_RE.search(a):
            drop2["翻唱伴奏黑名单"] += 1
            continue
        if len(t) > NAME_MAX or len(a) > ARTIST_MAX:
            drop2["名字异常"] += 1
            continue
        if not (MIN_DUR <= (s.get("d") or 0) <= MAX_DUR):
            drop2["时长超限"] += 1
            continue
        floor = POP_EST if a in strong else POP_NEW
        if (s.get("pop") or 0) < floor:
            drop2["无名低热"] += 1
            continue
        songs2[sid] = s
    log("质量闸门：%d → %d 首（实力歌手 %d 人；剔除：%s）"
        % (len(songs), len(songs2), len(strong),
           "、".join("%s %d" % (k, v) for k, v in drop2.items() if v)))
    songs = songs2

    now_ms = int(time.time() * 1000)
    arr = sorted(songs.values(), key=lambda x: -((x.get("pop") or 0) + _fresh_bonus(x.get("pt"), now_ms)))
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

    os.makedirs(CAT, exist_ok=True)
    for old in os.listdir(CAT):
        if old.startswith("shard-") or old.startswith("idx-") or old in ("manifest.json", "search.json"):
            os.remove(os.path.join(CAT, old))

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
    #   —— 前两段用内核同款 norm() 规则（小写+去空白/标点），用户输入什么都能搜到；
    #      后两段只用于「显示」，因为 norm 会吃掉空格与标点，不能拿它当标题给用户看。
    lines = []
    si = 0
    for n, s in enumerate(arr):
        if n and n % SHARD == 0:
            si += 1
        lines.append(SEP.join((_norm(s["n"]), _norm(s["a"]), str(s["i"]), str(si), s["n"], s["a"])))

    idx_files = []
    for i in range(0, len(lines), IDX_CHUNK):
        name = "idx-%02d.txt" % (i // IDX_CHUNK)
        with open(os.path.join(CAT, name), "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines[i:i + IDX_CHUNK]))
        idx_files.append(name)

    idx_bytes = sum(os.path.getsize(os.path.join(CAT, f)) for f in idx_files)
    shard_bytes = sum(os.path.getsize(os.path.join(CAT, s["file"])) for s in shards)
    man = {"updated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
           "count": len(arr), "shardSize": SHARD, "shards": shards,
           "idx": {"chunks": idx_files, "chunkSize": IDX_CHUNK, "total": len(lines),
                   "sep": SEP, "fields": ["nameN", "artistN", "id", "shard", "name", "artist"]},
           "schema": {"i": "songId", "n": "name", "a": "artist", "b": "album",
                      "p": "cover", "d": "duration_ms", "f": "fee",
                      "pop": "popularity0-100", "pt": "publishTime_ms"},
           "ranked": "pop + freshness", "sources": ["netease"]}
    json.dump(man, open(os.path.join(CAT, "manifest.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    log("分片完成：%d 片 / %d 首 / %.1f MB" % (len(shards), len(arr), shard_bytes / 1048576))
    log("索引完成：%d 块 / %d 行 / %.1f MB（每块 ~%.2f MB）"
        % (len(idx_files), len(lines), idx_bytes / 1048576,
           (idx_bytes / max(1, len(idx_files))) / 1048576))
    log("总体：曲库 %.1f MB + 索引 %.1f MB = %.1f MB"
        % (shard_bytes / 1048576, idx_bytes / 1048576, (shard_bytes + idx_bytes) / 1048576))


if __name__ == "__main__":
    cmd = (sys.argv[1] if len(sys.argv) > 1 else "enum").lower()
    {"enum": cmd_enum, "ids": cmd_ids, "artists": cmd_artists,
     "songs": cmd_songs, "pack": cmd_pack}[cmd]()
