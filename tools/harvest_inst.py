#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
伴奏采集器 —— 治本：让「伴奏」成为曲库里**真实存在的一类**，而不是端上靠关键词猜。

背景（2026-11-02 实测）：
  曲库 76,877 首里按关键词只能捞出 215 首伴奏（0.3%）—— 点「伴奏」几乎没东西。
  根因：采集器从来没把伴奏当成要采集的品类，只是听天由命等歌单里碰巧混进来。

做法：
  对「端上可见曲目」逐首去网易搜「<歌名> 伴奏」，三重闸门全过才落库：
    ① 标题必须含伴奏信号（伴奏/伴唱/karaoke/instrumental/off vocal/纯音乐…）
    ② 标题**不得**含翻唱信号（cover/翻唱/翻自/原唱:）—— 否则是把别人的翻唱当伴奏
    ③ 时长与原唱相差 ≤ TOL 秒（默认 10s）：伴奏时长≈原曲，差太多是另一首东西
  最后**真测**：Range GET + magic bytes，放不出来的不入库。

落库字段：
  cv=3  → 端上按「伴奏」独立呈现（不再靠正则猜）
  ov    → 指向原唱 songId（点伴奏时能跳回原唱）
  kin   → 伴奏对应的原曲 skey，端上可用它把伴奏挂到原曲卡片下

用法：
  python tools/harvest_inst.py --dry            # 只报数不写
  python tools/harvest_inst.py --limit 200      # 只跑最热 200 首
  python tools/harvest_inst.py                  # 全量
"""
import json, os, re, sys, time, urllib.request, urllib.parse, argparse
import concurrent.futures as cf

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import enrich_streams as E

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CAT = os.path.join(ROOT, "data", "catalog")
STREAM = os.path.join(ROOT, "data", "streams.json")
INST_FILE = os.path.join(ROOT, "data", "inst.json")

UA = {
    "User-Agent": ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 "
                   "(KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"),
    "Referer": "https://music.163.com/",
    "Cookie": "appver=2.9.7; os=pc",
}
OP = urllib.request.build_opener(urllib.request.ProxyHandler({}))
SEARCH = "https://music.163.com/api/cloudsearch/pc?s=%s&type=1&offset=0&limit=20"

# ① 伴奏信号（必须命中）
INST_RE = re.compile(
    r"(伴奏|伴唱|卡拉\s?ok|karaoke|instrumental|off\s?[\u00a0 ]?vocal|无人声|"
    r"纯音乐|纯音版|演奏版|消音|和声版)", re.I)
# ② 翻唱/串歌信号（命中即毙）
COVER_RE = re.compile(
    # ★ 2026-11-02 补「dj」「remix」「变速」的**任意写法**：实测漏网的
    #   「大漠长明(DJ乔大拿版)-伴奏」—— 正则只写了 `dj版`，挡不住 `DJ乔大拿版`。
    r"(cover|翻唱|翻自|原唱\s*[:：({（]|feat\.?\s|remix|dj|慢摇|变速|倍速|电音|鼓点)", re.I)
# ③ 明显不是伴奏的噪音
NOISE_RE = re.compile(r"(现场|live|演唱会|音乐节|清唱|清唱版|哼唱|试听|片段|铃声|彩铃)", re.I)


def get_json(u, timeout=15):
    for _ in range(2):
        try:
            r = OP.open(urllib.request.Request(u, headers=UA), timeout=timeout)
            return json.loads(r.read().decode("utf-8", "replace"))
        except Exception:
            time.sleep(0.6)
    return None


def dur_of(x):
    return int(x.get("dt") or x.get("duration") or 0) / 1000.0


def singer_of(x):
    ar = x.get("ar") or x.get("artists") or []
    return "/".join((z.get("name") or "") for z in ar)


def magic_ok(head):
    return (head[:3] == b"ID3"
            or (len(head) > 1 and head[0] == 0xFF and (head[1] & 0xE0) == 0xE0)
            or head[4:8] == b"ftyp"
            or head[:4] in (b"fLaC", b"OggS", b"RIFF"))


def probe(cid):
    """真测：网易外链能否拿到真音频"""
    u = "https://music.163.com/song/media/outer/url?id=%s.mp3" % cid
    try:
        r = OP.open(urllib.request.Request(u, headers=dict(UA, Range="bytes=0-4095")), timeout=20)
        ct = (r.headers.get("Content-Type") or "").lower()
        head = r.read(4096)
        if ct.startswith("text/"):
            return False, "html"
        if ct.startswith("audio/") or ct.startswith("video/"):
            return True, "ok"
        if magic_ok(head):
            return True, "magic"
        return False, "ctype:" + ct[:14]
    except Exception as e:
        return False, "err:" + repr(e)[:28]


def core_name(nm):
    """伴奏标题的「裸歌名」：去掉括号内容、去掉伴奏类词、抹掉空格。

    为什么要这个（2026-11-02 实测翻车）：
      旧判定是「互相包含即算过」，结果配出一堆错歌 ——
        「我的女人」→ 配到「下辈子做我的女人 伴奏」
        「千里之外」→ 配到「千里之外爱着你 (伴奏)」
      用户点的伴奏放出来是**另一首歌**，比没有伴奏更糟。
      现在要求裸歌名**完全相等**才收。
    """
    s = re.sub(r"[（(\[【][^）)\]】]*[）)\]】]", " ", nm or "")   # 去括号内容
    s = INST_RE.sub(" ", s)                                     # 去伴奏类词
    return E.norm(E.strip(s)).replace(" ", "")


def pick_inst(title, orig_dur, arr):
    """从搜索结果里挑最靠谱的伴奏。返回 (id, name, singer, dur) 或 None"""
    bt = core_name(title)
    best, bsc = None, 0
    for x in arr:
        nm = x.get("name") or ""
        if not INST_RE.search(nm):
            continue                                  # ① 没有伴奏信号 → 不是伴奏
        if COVER_RE.search(nm) or NOISE_RE.search(nm):
            continue                                  # ② 是翻唱/现场 → 毙
        ct = core_name(nm)
        # ★ 歌名必须**完全相等**（裸名对齐），绝不接受「互相包含」
        if not bt or ct != bt:
            continue
        sc = 100
        d = dur_of(x)
        # ③ 时长闸门
        if orig_dur and d:
            gap = abs(d - orig_dur)
            if gap <= 3:
                sc += 25
            elif gap <= 10:
                sc += 12
            elif gap <= 20:
                sc += 2
            else:
                continue
        if sc > bsc:
            bsc, best = sc, (str(x.get("id") or ""), nm, singer_of(x), int(d))
    return best if (best and best[0] and best[0] != "0") else None


def load_curated():
    return E.curated()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry", action="store_true", help="只报数不写库")
    ap.add_argument("--limit", type=int, default=0, help="只跑最热 N 首")
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--tol", type=int, default=10, help="时长容忍秒数")
    a = ap.parse_args()

    st = json.load(open(STREAM, encoding="utf-8"))
    smap = st.get("map") or {}
    inst = {}
    if os.path.exists(INST_FILE):
        try:
            inst = json.load(open(INST_FILE, encoding="utf-8"))
        except Exception:
            inst = {}
    cur = load_curated()
    todo = [(k, v) for k, v in cur.items() if k not in inst]
    todo.sort(key=lambda kv: -(kv[1].get("seen") or 0))
    if a.limit:
        todo = todo[:a.limit]
    print("待补伴奏 %d 首（已有 %d 首伴奏记录）｜%d 并发" % (len(todo), len(inst), a.workers), flush=True)

    stat = {"ok": 0, "no-result": 0, "no-match": 0, "dead": 0}
    got = {}
    t0 = time.time()

    def work(kv):
        k, v = kv
        q = (v.get("t") or "").strip()
        if not q:
            return k, None, "no-title"
        d = get_json(SEARCH % urllib.parse.quote(q + " 伴奏"))
        arr = (((d or {}).get("result") or {}).get("songs") or [])
        if not arr:
            return k, None, "no-result"
        p = pick_inst(q, (v.get("dur") or 0), arr)
        if not p:
            return k, None, "no-match"
        ok, why = probe(p[0])
        if not ok:
            return k, None, "dead:" + why
        return k, {"i": p[0], "n": p[1], "a": p[2], "d": int(p[3] * 1000),
                   "ov": smap.get(k, {}).get("wy") or 0, "kin": k, "cv": 3}, "ok"

    with cf.ThreadPoolExecutor(a.workers) as ex:
        for n, (k, rec, why) in enumerate(ex.map(work, todo), 1):
            key = why.split(":")[0]
            stat[key] = stat.get(key, 0) + 1
            if rec:
                got[k] = rec
            if n % 25 == 0:
                print("  … %d/%d（补到 %d 首，%.0fs）" % (n, len(todo), len(got), time.time() - t0), flush=True)

    print("\n=== 伴奏采集 ===")
    print("  补到 %d 首伴奏" % len(got))
    for k, v in sorted(stat.items(), key=lambda x: -x[1]):
        print("    %-10s %d" % (k, v))
    if a.dry:
        print("  （--dry：未写库）样例：")
        for k in list(got)[:8]:
            r = got[k]
            print("    %s | %s → %s | %s (%ds)" % (cur[k].get("t"), cur[k].get("s"), r["n"], r["a"], r["d"] // 1000))
        return

    inst.update(got)
    tmp = INST_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(inst, fh, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, INST_FILE)
    print("  已写入 %s（累计 %d 首伴奏）" % (os.path.relpath(INST_FILE, ROOT), len(inst)))


if __name__ == "__main__":
    main()
