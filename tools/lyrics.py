# -*- coding: utf-8 -*-
"""LRCLIB 歌词补齐流水线：把带时间轴的 LRC 落成 CDN 分片，端上按需取。

================================ 为什么在云端跑 ================================
2026-10-22 本机实测（国内家宽直连 lrclib.net）：
    · /api/get 单请求 5.8s，40 首串行跑 15 分钟还没完
    · 并发 6 → 22×URLError(RST) + 18×503，等于被当场打回
    → 端上「边播边查」绝无可能（用户早就播完了还在转圈）。
  所以走：**GitHub Actions（海外网络）串行预取 → 落 data/lyrics/ 分片 → jsDelivr → 端上读静态分片**。
  端上只在分片里没有时，才用原生 fetch 兜底查一次（带本地缓存，失败静默）。

================================ 官方 fair-use（照做，别撞限流）================================
    · 串行请求，间隔 200~500ms
    · User-Agent 必须带应用名+版本+项目页（已标识 90 次/分，未标识只 45）
    · 429 必须遵守 Retry-After + 30% 抖动
    · /api/get 的 duration 容差 **±2 秒**，所以默认**不传** duration（传不准反而 404）
    · /api/search 实测 34.7s 超时，不用

================================ 产物 ================================
    data/lyrics/manifest.json      {updated, count, shardSize, shards:[{file,count}]}
    data/lyrics/shard-XXXX.json    { "<skey>": {"l": "<LRC 全文>", "d": 时长秒, "s": "lrclib" } }
  skey 与曲库同一套（tools/skey_contract.json），端上 song.skey() 直接查。

用法：
    python tools/lyrics.py                     # 增量补齐（跳过已有）
    python tools/lyrics.py --limit 3000        # 本轮最多查 3000 首
    python tools/lyrics.py --budget 1500       # 本轮最多跑 1500 秒（CI 里防超时）
    python tools/lyrics.py --top 20000         # 只补热度前 2 万首（默认）
    python tools/lyrics.py --dry               # 只统计不请求
"""
import argparse
import json
import os
import random
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)                     # .../cloud
CAT = os.path.join(ROOT, "data", "catalog")
LYR = os.path.join(ROOT, "data", "lyrics")

UA = {"User-Agent": "DawgeMusic/1.0.0 (https://github.com/lidawei1985/ios-music-app)"}
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
SHARD = 500
GAP = (0.25, 0.45)          # 官方建议 200~500ms

# —— 复用曲库构建的判定规则（与 build.py 保持一致，避免「补了不该补的」）——
JUNK_TITLE_RE = re.compile(
    r"(4K修复|8K|Hi-Res|无损音质|超清|字幕|完整版|合集|剪辑|解说|reaction|纯享|车载|"
    r"DJ版|加速版|慢速版|消音|伴奏版|卡拉OK|KTV|幕后|花絮)", re.I)
BAD_ARTIST = re.compile(r"^(群星|未知|佚名|网络歌手|unknown|various.*)$", re.I)
SUFFIX = re.compile(r"[\(（\[【].*?[\)）\]】]", re.S)
JUNK_ARTIST = re.compile(r"\d{5,}|ㅤ|^[-_·\s]+$")


def norm(s):
    return re.sub(r"[\s\-_（）()\[\]【】·,.，。'\"!！?？~～&]", "", (s or "")).lower()


def cpslice(s, n):
    """按码点截断（与端上 Array.from 一致；直接切片会把 emoji 切成半个）"""
    return "".join(list(str(s or ""))[:n])


def skey(t, s):
    return cpslice(norm(t), 40) + "|" + cpslice(norm(s), 30)


def clean(s):
    s = SUFFIX.sub("", (s or "").strip())
    return re.sub(r"\s+", " ", s).strip()


# ------------------------------------------------------------------ 曲库读取
def load_catalog(top):
    mf = os.path.join(CAT, "manifest.json")
    if not os.path.exists(mf):
        sys.exit("找不到 %s" % mf)
    man = json.load(open(mf, encoding="utf-8"))
    items = []
    for sh in man["shards"]:
        p = os.path.join(CAT, sh["file"])
        if os.path.exists(p):
            items.extend(json.load(open(p, encoding="utf-8")))
    print("曲库 %d 首" % len(items))

    # 只补「值得补」的：原唱、有歌手、非垃圾标题、非纯音乐
    keep = []
    for x in items:
        a, n = (x.get("a") or "").strip(), (x.get("n") or "").strip()
        if not a or not n or BAD_ARTIST.match(a) or JUNK_ARTIST.search(a):
            continue
        if JUNK_TITLE_RE.search(n) or x.get("cv") == 2:
            continue
        if x.get("inst"):
            continue
        keep.append(x)
    print("可补歌词 %d 首（剔掉群星/垃圾标题/翻唱/伴奏）" % len(keep))

    # 热度优先：先把大家真会点的歌补齐
    keep.sort(key=lambda x: -(x.get("pop") or 0))
    return keep[:top]


# ------------------------------------------------------------------ 已有歌词
def load_done():
    """返回已落盘的 key 集合 + 歌词字典，支持增量。"""
    done = {}
    mf = os.path.join(LYR, "manifest.json")
    if not os.path.exists(mf):
        return done
    try:
        man = json.load(open(mf, encoding="utf-8"))
    except Exception:
        return done
    for sh in man.get("shards", []):
        p = os.path.join(LYR, sh["file"])
        if os.path.exists(p):
            try:
                done.update(json.load(open(p, encoding="utf-8")))
            except Exception:
                pass
    return done


# ------------------------------------------------------------------ 请求
def fetch(a, t, dur=None, retries=2):
    q = {"artist_name": a, "track_name": t}
    # 默认不传 duration：官方容差 ±2s，而曲库时长来自 QQ/酷狗未必准，
    # 传进去只会把本来存在的歌词变成 404。命中后自己比时长。
    if dur:
        q["duration"] = "%d" % round(dur)
    url = "https://lrclib.net/api/get?" + urllib.parse.urlencode(q)
    for attempt in range(retries + 1):
        try:
            with OPENER.open(urllib.request.Request(url, headers=UA), timeout=25) as r:
                return json.loads(r.read().decode("utf-8")), None
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None, "404"
            if e.code == 429:
                wait = float(e.headers.get("Retry-After") or 5)
                if attempt < retries:
                    time.sleep(wait + random.uniform(0, wait * 0.3))
                    continue
                return None, "429"
            if e.code >= 500 and attempt < retries:
                time.sleep(1.5 * (attempt + 1))
                continue
            return None, "HTTP%d" % e.code
        except Exception as e:
            if attempt < retries:
                time.sleep(1.2 * (attempt + 1))
                continue
            return None, type(e).__name__
    return None, "retry"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--top", type=int, default=20000, help="只补热度前 N 首")
    ap.add_argument("--limit", type=int, default=0, help="本轮最多查多少首（0=不限）")
    ap.add_argument("--budget", type=int, default=1500, help="本轮时间预算（秒）")
    ap.add_argument("--dry", action="store_true")
    ap.add_argument("--save-every", type=int, default=400)
    ap.add_argument("--rpm", type=int, default=80,
                    help="每分钟请求上限（官方：已标识 UA 约 90；留 10 的余量防抖动）")
    args = ap.parse_args()

    # 硬限速：间隔 = max(官方建议下限, 60/rpm)。海外 runner RTT 低，
    # 不带这个闸门会瞬间冲到几百次/分直接吃 429。
    min_gap = 60.0 / max(1, args.rpm)

    pool = load_catalog(args.top)
    done = load_done()
    print("已有歌词 %d 条" % len(done))

    todo = [x for x in pool if skey(x["n"], x["a"]) not in done]
    print("待补 %d 首" % len(todo))
    if args.limit:
        todo = todo[:args.limit]
        print("本轮限 %d 首" % len(todo))
    if args.dry or not todo:
        return

    t0 = time.time()
    stat = Counter()
    got = []
    try:
        for i, x in enumerate(todo, 1):
            if time.time() - t0 > args.budget:
                print("\n⏳ 到时间预算 %ds，收工（下轮继续）" % args.budget)
                break
            a, n = clean(x["a"]), clean(x["n"])
            libdur = (x.get("d") or 0) / 1000.0
            rec, err = fetch(a, n)
            if not rec:
                stat[err or "?"] += 1
            elif rec.get("instrumental"):
                stat["纯音乐"] += 1
            elif not (rec.get("syncedLyrics") or "").strip():
                stat["无时间轴"] += 1
            else:
                ld = rec.get("duration") or 0
                drift = abs(ld - libdur) if (ld and libdur) else 0
                if drift > 8:
                    # 名字对了但时长差太多 → 大概率同名不同曲，宁缺毋滥
                    stat["时长错配"] += 1
                else:
                    stat["命中"] += 1
                    got.append((skey(x["n"], x["a"]),
                                {"l": rec["syncedLyrics"], "d": round(ld or libdur, 1),
                                 "s": "lrclib"}))
                    done[skey(x["n"], x["a"])] = got[-1][1]
            if i % 50 == 0:
                el = time.time() - t0
                print("  %d/%d ｜命中 %d (%.0f%%) ｜%.1fs ｜%.1f 首/分" % (
                    i, len(todo), stat["命中"],
                    100.0 * stat["命中"] / i, el, i / el * 60))
            if got and len(got) >= args.save_every:
                flush(got, done)
                got = []
            time.sleep(max(random.uniform(*GAP), min_gap))
    except KeyboardInterrupt:
        print("\n中断，保存已拿到的")
    if got:
        flush(got, done)

    el = time.time() - t0
    n = sum(stat.values())
    print("\n=== 本轮 ===")
    print("请求 %d 首 ｜ 耗时 %.0fs ｜ 有效 %.1f 首/分" % (n, el, n / max(el, 1) * 60))
    if n:
        print("命中 %d（%.1f%%）" % (stat["命中"], 100.0 * stat["命中"] / n))
    for k, v in stat.most_common():
        if k != "命中":
            print("  %-10s %d" % (k, v))
    print("累计歌词 %d 条 → %s" % (len(done), LYR))


def flush(new_items, allmap):
    """把新拿到的歌词合并进分片并重写 manifest。"""
    os.makedirs(LYR, exist_ok=True)
    keys = sorted(allmap.keys())
    shards = []
    for i in range(0, max(1, len(keys)), SHARD):
        chunk = keys[i:i + SHARD]
        name = "shard-%04d.json" % (i // SHARD)
        obj = {k: allmap[k] for k in chunk}
        with open(os.path.join(LYR, name), "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, separators=(",", ":"))
        shards.append({"file": name, "count": len(chunk)})
    man = {"updated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
           "count": len(keys), "shardSize": SHARD, "shards": shards}
    with open(os.path.join(LYR, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump(man, f, ensure_ascii=False, indent=1)
    print("   💾 落盘 %d 条 / %d 片" % (len(keys), len(shards)))


if __name__ == "__main__":
    main()
