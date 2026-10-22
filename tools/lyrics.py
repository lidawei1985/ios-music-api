# -*- coding: utf-8 -*-
"""歌词补齐流水线：云端预取带时间轴的 LRC → CDN 分片 → 端上按需取。

================================ 主源换人（2026-10-22 实测后改的）================================
最初按「用 LRCLIB 对齐」的设想只接了 LRCLIB，结果用**真实榜单 80 首**一测：
    · LRCLIB  带时间轴命中 **4/80 = 5.0%**，487s（约 6.1s/首）
    · 网易云 wy_lyric 抽样 15 首命中 **10/15 = 67%**，1.9s（**0.13s/首**）
  → LRCLIB 是西方社区主导，中文流行歌大面积 404；快 40 倍、准 13 倍的网易云才是主力。
  现在：**网易云 id 优先 → LRCLIB 兜底**（只对没有网易云 id 的 kw/migu 曲目才有意义）。

================================ 为什么在云端跑 ================================
2026-10-22 本机（国内家宽）实测 lrclib.net：
    · 单请求 5.8s；40 首串行跑 33 分钟；并发 6 → 22×URLError(RST) + 18×503
    → 端上「边播边查」不可能。云端预取 → data/lyrics/js/ 分片 → 端上读静态分片。

================================ 官方 fair-use（LRCLIB 部分）================================
    · 串行 + 间隔 200~500ms；已标识 UA 约 90 次/分，本流水线自限 80
    · 429 的 Retry-After 封顶 8s（照睡会把整轮预算吃光，实测踩过）
    · /api/get 的 duration 容差只有 ±2s → 默认不传
    · /api/search 实测 34.7s 超时，彻底不用

================================ 产物 ================================
    data/lyrics/js/shard-XXXX.js   window.__DWG_LYRS[i] = {"<skey>": {"l": LRC, "d": 秒, "s": 源}}
    data/lyrics/js/m.js            window.__DWG_LYRMAN   = {updated, count, shardSize, shards[{file,count,from,to}]}
    data/lyrics/manifest.json      人/脚本查看用（端上不读）
  .js 而不是 .json：App 页面是 file://，这种文档下 fetch/XHR 跨源被拦死，**只放行 script/img**。

用法：
    python tools/lyrics.py                     # 增量补齐（跳过已有）
    python tools/lyrics.py --limit 3000
    python tools/lyrics.py --budget 1500
    python tools/lyrics.py --top 20000         # 只补热度前 2 万首（默认）
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

    # ★ 排序规则（2026-10-22 实测后改）：
    #   曲库的 pop 字段在部分分片里普遍虚高，纯按 pop 取前 N 会取到一大堆
    #   儿歌/催眠曲/地下说唱 —— 那批 LRCLIB 命中 5%、网易云也大面积无词，
    #   跑一轮只捞到 2 条（实测）。所以先按「有没有网易云 id」分组：
    #   有 wy id 的曲目才是能真正拿到官方歌词的主体（占曲库 98%）。
    def prio(x):
        src0 = (x.get("src") or "wy")
        has_wy = 1 if (src0 in ("wy", "", None) and x.get("i")) else 0
        return (-has_wy, -(x.get("pop") or 0))

    keep.sort(key=prio)
    return keep if top <= 0 else keep[:top]


# ------------------------------------------------------------------ 已有歌词
def load_done():
    """读回已落盘的歌词（增量基础）。

    产物是 .js（见 flush 的说明），所以要剥掉 `window.__DWG_LYRS[...]=` 外壳再 json 解析。
    剥不掉就跳过这一片 —— 宁可重查几十首，也不要因为一片坏了整轮失败。
    """
    done = {}
    mf = os.path.join(LYR, "manifest.json")
    if not os.path.exists(mf):
        return done
    try:
        man = json.load(open(mf, encoding="utf-8"))
    except Exception:
        return done
    for sh in man.get("shards", []):
        name = sh.get("file") or ""
        p = os.path.join(LYR, "js", name)
        if not os.path.exists(p):
            # 兼容早期的 .json 分片（第一版产物）
            alt = os.path.join(LYR, name.replace(".js", ".json"))
            if os.path.exists(alt):
                try:
                    done.update(json.load(open(alt, encoding="utf-8")))
                except Exception:
                    pass
            continue
        try:
            txt = open(p, encoding="utf-8").read()
            # 只认外壳的 `window.__DWG_LYRS[<n>=` 收尾位置，别用 find("]=")
            # —— 歌词正文里完全可能出现 `]=`，那样会从中间截断解析失败
            m = re.search(r"window\.__DWG_LYRS\[\d+\]=", txt)
            if not m:
                continue
            body = txt[m.end():].rstrip()
            if body.endswith(";"):
                body = body[:-1]
            done.update(json.loads(body))
        except Exception:
            pass
    return done


# ------------------------------------------------------------------ 请求
def wy_lyric(sid):
    """网易云歌词（主源）。返回 (lrc, trans) 或 (None, None)。

    ★ 实测 0.13s/首、67% 命中 —— 比 LRCLIB 快 40 倍、准 13 倍。
      中文歌的官方词基本都在这里，还额外带翻译（tv=-1）。
    """
    u = "https://music.163.com/api/song/lyric?id=%s&lv=1&kv=0&tv=-1" % sid
    for attempt in range(2):
        try:
            with OPENER.open(urllib.request.Request(u, headers={
                    "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X)",
                    "Referer": "https://music.163.com/"}), timeout=15) as r:
                d = json.loads(r.read().decode("utf-8", "replace"))
            return (((d.get("lrc") or {}).get("lyric") or "").strip(),
                    ((d.get("tlyric") or {}).get("lyric") or "").strip())
        except Exception:
            if attempt == 0:
                time.sleep(0.6)
                continue
            return None, None
    return None, None


def fetch(a, t, dur=None, retries=2):
    q = {"artist_name": a, "track_name": t}
    # 默认不传 duration：官方容差 ±2s，而曲库时长来自 QQ/酷狗未必准，
    # 传进去只会把本来存在的歌词变成 404。命中后自己比时长。
    if dur:
        q["duration"] = "%d" % round(dur)
    url = "https://lrclib.net/api/get?" + urllib.parse.urlencode(q)
    for attempt in range(retries + 1):
        try:
            with OPENER.open(urllib.request.Request(url, headers=UA), timeout=20) as r:
                return json.loads(r.read().decode("utf-8")), None
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None, "404"
            if e.code == 429:
                # ★ 2026-10-22 血案：官方 Retry-After 可能是几百秒，照睡会把
                #   一轮 15 分钟的预算全吃光（首轮 CI 就这样跑了 33 分钟没退出）。
                #   这里封顶 8s，剩下的交给「连续 429 熔断」——被限流就早点收工，
                #   宁可这轮少补几百首，也不要挂满一小时的 job。
                wait = min(float(e.headers.get("Retry-After") or 5), 8.0)
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
    ap.add_argument("--lrclib", action="store_true",
                    help="启用 LRCLIB 兜底（默认关：真实榜单实测命中仅 5%%，"
                         "却比网易云慢 40 倍，开着会把整轮速度拖到 1/20）")
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
    streak429 = 0                  # 连续限流计数 → 熔断
    try:
        for i, x in enumerate(todo, 1):
            if time.time() - t0 > args.budget:
                print("\n⏳ 到时间预算 %ds，收工（下轮继续）" % args.budget)
                break
            if streak429 >= 12:
                print("\n🛑 连续 12 次被限流 → 提前收工（别把 job 挂在等 Retry-After 上）")
                break
            a, n = clean(x["a"]), clean(x["n"])
            libdur = (x.get("d") or 0) / 1000.0
            k = skey(x["n"], x["a"])

            # ① 网易云优先（主源：0.13s/首，67% 命中，带翻译）
            hit_done = False
            src0 = (x.get("src") or "wy")
            wyid = str(x.get("i") or "") if src0 in ("wy", "", None) else ""
            if wyid:
                lrc, trans = wy_lyric(wyid)
                if lrc and re.search(r"\[\d{1,3}:\d{1,2}", lrc):
                    stat["命中(网易云)"] += 1
                    stat["命中"] += 1
                    rec_v = {"l": lrc, "d": round(libdur, 1), "s": "wy"}
                    if trans:
                        rec_v["t"] = trans
                    done[k] = rec_v
                    got.append((k, rec_v))
                    hit_done = True
                elif lrc:
                    stat["网易云无时间轴"] += 1
                else:
                    stat["网易云无词"] += 1
            else:
                stat["无网易云id"] += 1

            # ② LRCLIB 兜底（默认关；只有 --lrclib 才走）。
            #    理由：真实榜单 80 首实测只命中 4 首（5%），却要 6.1s/首，
            #    开着会把「网易云 0.2s/首」拖成「4.6s/首」，整轮产出反而更少。
            if not hit_done and args.lrclib:
                rec, err = fetch(a, n)
                streak429 = streak429 + 1 if err == "429" else 0
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
                        stat["命中(LRCLIB)"] += 1
                        stat["命中"] += 1
                        rec_v = {"l": rec["syncedLyrics"], "d": round(ld or libdur, 1), "s": "lrclib"}
                        done[k] = rec_v
                        got.append((k, rec_v))
            if i % 50 == 0:
                el = time.time() - t0
                print("  %d/%d ｜命中 %d (%.0f%%) ｜%.1fs ｜%.1f 首/分" % (
                    i, len(todo), stat["命中"],
                    100.0 * stat["命中"] / i, el, i / el * 60))
            if got and len(got) >= args.save_every:
                flush(got, done)
                got = []
            # 网易云命中时只做轻量间隔（实测 0.13s/首都稳），
            # 只有真去打 LRCLIB 才套 80 次/分的硬限速 —— 否则 7.7 万首会被限速拖成 16 小时。
            if hit_done:
                time.sleep(random.uniform(0.05, 0.12))
            else:
                time.sleep(max(random.uniform(*GAP), min_gap))
    except KeyboardInterrupt:
        print("\n中断，保存已拿到的")
    if got:
        flush(got, done)

    el = time.time() - t0
    stat["命中"] = stat["命中(网易云)"] + stat["命中(LRCLIB)"]
    n = sum(v for k, v in stat.items() if k != "命中")   # 「命中」是两者之和，别重复计
    print("\n=== 本轮 ===")
    print("请求 %d 首 ｜ 耗时 %.0fs ｜ 有效 %.1f 首/分" % (n, el, n / max(el, 1) * 60))
    if n:
        print("命中 %d（%.1f%%）" % (stat["命中"], 100.0 * stat["命中"] / n))
    for k, v in stat.most_common():
        if k != "命中":
            print("  %-10s %d" % (k, v))
    print("累计歌词 %d 条 → %s" % (len(done), LYR))


def flush(new_items, allmap):
    """把新拿到的歌词合并进分片并重写清单。

    ★★ 为什么产物是 **.js 不是 .json**（2026-10-22，和曲库 cat-manifest.js 同一套理由）：
       App 里页面是 file://（WKWebView / Android WebView 载入沙盒内核），
       这种文档下 fetch/XHR 跨源会被拦死，**只放行 <script> 与 <img> 子资源**。
       所以分片必须是能被 <script src> 直接吃的形状：
           window.__DWG_LYRS[<i>] = { "<skey>": {l,d,s}, ... }
       清单同理：window.__DWG_LYRMAN = {updated,count,shardSize,shards}

    ★ 分片带 key 区间（from/to）：端上不拉全量索引、也不全量下载，
      拿清单里的区间二分一次就能定位到**唯一一片**，只下 ~1.5MB。
      分片内部按 key 排序（sorted(keys) 切），所以区间连续且无重叠。
    """
    jsdir = os.path.join(LYR, "js")
    os.makedirs(jsdir, exist_ok=True)
    keys = sorted(allmap.keys())
    shards = []
    for i in range(0, max(1, len(keys)), SHARD):
        chunk = keys[i:i + SHARD]
        name = "shard-%04d.js" % (i // SHARD)
        payload = json.dumps({k: allmap[k] for k in chunk},
                             ensure_ascii=False, separators=(",", ":"))
        with open(os.path.join(jsdir, name), "w", encoding="utf-8") as f:
            # 一次赋值，不要 JSONP 回调 —— 端上用 loadScript 等 onload 再读全局量
            f.write("window.__DWG_LYRS=window.__DWG_LYRS||{};"
                    "window.__DWG_LYRS[%d]=%s;\n" % (i // SHARD, payload))
        shards.append({"file": name, "count": len(chunk),
                       "from": chunk[0], "to": chunk[-1]})
    man = {"updated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
           "count": len(keys), "shardSize": SHARD, "shards": shards}
    with open(os.path.join(jsdir, "m.js"), "w", encoding="utf-8") as f:
        f.write("window.__DWG_LYRMAN=%s;\n"
                % json.dumps(man, ensure_ascii=False, separators=(",", ":")))
    # manifest.json 只留作人/脚本查看（端上不读它）
    with open(os.path.join(LYR, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump(man, f, ensure_ascii=False, indent=1)
    print("   💾 落盘 %d 条 / %d 片 → data/lyrics/js/" % (len(keys), len(shards)))


if __name__ == "__main__":
    main()
