# -*- coding: utf-8 -*-
"""策展曲目 → 真 id（离线补源，零网络，秒级）

============================ 为什么必须有它 ============================
2026-10-30 现场实测找到的「点了没反应 / 一首接一首跳过」真根因：

    App 里最显眼的四个入口（首页热歌 / 榜单 / 主视觉海报 / 我的）
    用的全是**策展数据**（charts.json / hero.json / library.json /
    categories.json）。策展数据里一首歌只有 `title/singer/cover` + 一个
    **QQ 的 mid** —— 没有任何 wy/kw/migu 的可播 id。
    于是端上 stRec() 取不到 id → srcUrl() 返回空 → 「暂无可用音源」→ 自动跳歌。

    负责把歌名翻译成 id 的 build_streams() 走的是 multi_match（联网 + 真测播放），
    慢且贵：每轮有时间预算，增量跑到今天也只覆盖一千来首 —— 剩下那些
    在 App 上就是「点了不出声」。

============================ 这一步做什么 ============================
其实答案早就躺在自己的仓库里：**data/catalog 就是 76,954 首带实测可播 id 的曲库**，
而 index 文件（idx-*.txt）里现成就有 歌名/歌手/id/src。
所以绝大多数策展曲目根本不用联网，查一下本机曲库就能翻译出来。

    离线命中 → 直接写 streams.json（毫秒级，不耗 Actions 分钟数）
    离线没中 → 留给 build_streams 的联网匹配（增量续跑，一轮补一点）

两者是互补不是替代：本脚本覆盖「曲库里有的」，build_streams 补「曲库里没有的」。

产物形态与 build_streams 完全一致（{"wy": id, "src": "wy", ...}），
端上 stRec() 按 skey 查 DB.st 就能直接出声，**不需要改 App**。

用法：
    python tools/enrich_streams.py            # 增量补源
    python tools/enrich_streams.py --dry      # 只看能补多少
"""
import argparse
import json
import os
import re
import sys
import time

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
CAT = os.path.join(ROOT, "data", "catalog")
DATA = os.path.join(ROOT, "data")

DROP = re.compile(r"[\s\-_（）()\[\]【】·,.，。'\"!！?？~～&]")
SUF = re.compile(r"[\(（\[【][^\)）\]】]{0,30}[\)）\]】]")
SEP = "\x01"


def norm(x):
    return DROP.sub("", str(x or "")).lower()


def strip(x):
    return norm(SUF.sub(" ", str(x or "")))


def skey(t, s):
    return norm(t)[:40] + "|" + norm(s)[:30]


def sid(raw):
    """≤15 位纯数字才转 int，其余当不透明字符串（与端上同一把尺子）"""
    raw = str(raw or "").strip()
    return int(raw) if re.match(r"^\d{1,15}$", raw) else raw


# ------------------------------------------------------------------ 1) 载入曲库索引
def load_catalog():
    """返回 [(nameN, artistN, id, src, name, artist, cv)] —— 只读索引，不读分片"""
    man_p = os.path.join(CAT, "manifest.json")
    if not os.path.exists(man_p):
        return []
    man = json.load(open(man_p, encoding="utf-8"))
    out = []
    for ch in ((man.get("idx") or {}).get("chunks") or []):
        p = os.path.join(CAT, ch)
        if not os.path.exists(p):
            continue
        with open(p, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.rstrip("\n").rstrip("\r")
                if not line:
                    continue
                fs = line.split(SEP)
                if len(fs) < 3:
                    continue
                i = sid(fs[2])
                if not i:
                    continue
                out.append((fs[0], fs[1], i, (fs[8] if len(fs) > 8 else "").strip() or "wy",
                            fs[4] if len(fs) > 4 else fs[0],
                            fs[5] if len(fs) > 5 else fs[1],
                            int(fs[6] or 0) if len(fs) > 6 else 0))
    return out


# ------------------------------------------------------------------ 2) 待补的策展曲目
def curated():
    """复刻 build.py::_collect_songs 的口径，拿到同样那批 key"""
    acc = {}

    def put(t, s, dur=0, seen=0):
        if not t:
            return
        k = skey(t, s)
        if k == "|":
            return
        e = acc.get(k)
        if e is None:
            acc[k] = {"t": t, "s": s or "", "dur": dur or 0, "seen": seen or 0}
        else:
            e["seen"] = max(e["seen"], seen or 0)

    def rd(name):
        p = os.path.join(DATA, name)
        return json.load(open(p, encoding="utf-8")) if os.path.exists(p) else {}

    lib = rd("library.json")
    for s in (lib.get("songs") or []):
        put(s.get("t"), s.get("s"), s.get("dur"), s.get("seen"))
    charts = rd("charts.json")
    for l in (charts.get("lists") or []):
        for s in (l.get("songs") or []):
            put(s.get("title"), s.get("singer"), s.get("duration"), 1)
    cats = rd("categories.json")
    for g in (cats.get("groups") or []):
        for c in (g.get("cats") or []):
            for s in (c.get("songs") or []):
                put(s.get("t"), s.get("s"), s.get("dur"), 1)
    hero = rd("hero.json")
    for s in (hero.get("items") or []):
        put(s.get("t"), s.get("s"), s.get("dur"), 99)
    return acc


# ------------------------------------------------------------------ 3) 匹配
def match(idx_by_title, t, s):
    """严格优先原唱：歌名完全一致 > 剥后缀后一致；歌手必须沾边；原唱优先"""
    nt, ns = norm(t), norm(s)
    st = strip(t)
    best, bs = None, -99
    for cand in (idx_by_title.get(nt) or []) + (idx_by_title.get(st) or []):
        ctn, cts = cand[0], strip(cand[4])
        sc = -99
        if ctn == nt:
            sc = 6
        elif st and cts == st:
            sc = 3
        if sc < -90:
            continue
        cn = cand[1]
        if ns and cn:
            if cn == ns:
                sc += 5
            elif cn in ns or ns in cn:
                sc += 2
            else:
                continue                      # 同名不同人 → 绝不能串
        if cand[6] == 1:
            sc += 3                           # 原唱优先
        elif cand[6] == 2:
            sc -= 3                           # 翻唱靠后
        if sc > bs:
            bs, best = sc, cand
    return best


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry", action="store_true")
    args = ap.parse_args()

    rows = load_catalog()
    if not rows:
        print("❌ 拿不到曲库索引（data/catalog/manifest.json 缺失或没有 idx 块）")
        return 1
    by_title = {}
    for r in rows:
        by_title.setdefault(r[0], []).append(r)
        by_title.setdefault(strip(r[4]), []).append(r)
    print("曲库索引 %d 条；去重标题 %d 个" % (len(rows), len(by_title)))

    acc = curated()
    print("策展曲目去重 %d 首" % len(acc))

    sf = os.path.join(DATA, "streams.json")
    st = json.load(open(sf, encoding="utf-8")) if os.path.exists(sf) else {}
    omap = dict(st.get("map") or {})
    _m = st.get("miss")
    # 历史产物里 miss 曾是 int（只是个计数）；新格式是 {key: 连败轮数}
    miss = dict(_m) if isinstance(_m, dict) else {}
    before = len(omap)

    t0 = time.time()
    added = 0
    for k, e in acc.items():
        if k in omap:
            continue
        hit = match(by_title, e["t"], e["s"])
        if not hit:
            continue
        i, src = hit[2], hit[3]
        if isinstance(i, str) and src == "wy":
            pass
        rec = {"src": src, "n": hit[4], "a": hit[5], "native": 1, "dur": e.get("dur") or 0}
        if src == "wy":
            rec["wy"] = i
        else:
            rec[src] = i
        omap[k] = rec
        miss.pop(k, None)
        added += 1
    print("离线补源 %d 首 ← 本机曲库（%.1fs）" % (added, time.time() - t0))

    hit = sum(1 for k in acc if k in omap)
    srcs = {}
    for v in omap.values():
        srcs[v.get("src") or "wy"] = srcs.get(v.get("src") or "wy", 0) + 1
    out = {
        "updated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "count": len(acc), "hit": hit,
        "rate": round(100.0 * hit / max(1, len(acc)), 1),
        "native": sum(1 for v in omap.values() if v.get("native")),
        "srcs": srcs, "miss": miss,
        "note": (st.get("note")
                 or ("key = 归一化(歌名)|归一化(歌手)。值存长效 id（不是直链，直链带签名会过期）。"
                     "本文件由 build.py 的联网实测匹配 + enrich_streams.py 的离线曲库翻译共同维护。")),
        "map": omap,
    }
    print("可播覆盖 %s/%s = %s%%（本步之前 %s 首）｜来源 %s"
          % (hit, len(acc), out["rate"], before,
             "、".join("%s %d" % (k, v) for k, v in sorted(srcs.items()))))
    if args.dry or added == 0:
        print("--dry：不写盘" if added else "没有新增")
        return 0
    json.dump(out, open(sf, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print("💾 data/streams.json 已更新")
    return 0


if __name__ == "__main__":
    sys.exit(main())
