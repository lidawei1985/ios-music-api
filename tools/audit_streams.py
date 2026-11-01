# -*- coding: utf-8 -*-
r"""★ 音源「串歌」体检 —— 把已落库的 wy id 反查回网易，核对到底是谁的歌。

============================= 为什么必须有这一步 ==============================
2026-11-02 实测翻案：补源脚本读网易 `cloudsearch/pc` 时用的是老接口的字段名
`artists` / `duration`，而该接口真实返回的是 **`ar`** / **`dt`** —— 两个值恒空，
于是「歌手校验」与「时长校验」两道关卡**全程被静默跳过**，只要歌名一样就落库。
后果不是"少补几首"，而是**放出来的是别人的版本**（用户反复投诉的「歌不对」）。

本脚本不猜：拿 streams.json 里每条 wy id 去网易官方详情接口**反查真名**，
与曲库记录的「歌名|歌手」逐条对账，把对不上的标记出来（默认 --fix 直接清除，
让它回到"缺源"状态，由修好的补源脚本重新补 —— 宁缺勿错）。

用法：
    python tools/audit_streams.py --dry          # 只报数
    python tools/audit_streams.py --limit 300    # 抽样
    python tools/audit_streams.py --fix          # 清除串歌条目并写盘
"""
import argparse
import json
import os
import sys
import time
import urllib.parse
import urllib.request

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import enrich_streams as E                       # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DATA = os.path.join(ROOT, "data")
STREAM = os.path.join(DATA, "streams.json")

UA = {"User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
                    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 "
                    "Mobile/15E148 Safari/604.1",
      "Referer": "https://music.163.com/",
      "Cookie": "appver=2.9.7; os=pc"}
DETAIL = "https://music.163.com/api/v3/song/detail?c=%s"
OP = urllib.request.build_opener(urllib.request.ProxyHandler({}))
BATCH = 40


def detail(ids):
    """反查：id -> {name, singer, dur}。拿不到返回 None（不猜）。"""
    c = json.dumps([{"id": int(i)} for i in ids], ensure_ascii=False)
    for attempt in range(3):
        try:
            d = json.loads(OP.open(urllib.request.Request(
                DETAIL % urllib.parse.quote(c), headers=UA), timeout=20
            ).read().decode("utf-8", "replace"))
            out = {}
            for x in (d.get("songs") or []):
                sid = str(x.get("id") or "")
                raw = x.get("dt")
                if raw is None:
                    raw = x.get("duration")
                try:
                    raw = int(raw or 0)
                except Exception:
                    raw = 0
                dur = raw / 1000.0 if raw > 100000 else float(raw)
                out[sid] = {
                    "t": x.get("name") or "",
                    "s": "/".join((z.get("name") or "")
                                  for z in (x.get("ar") or x.get("artists") or [])),
                    "dur": dur,
                }
            return out
        except Exception:
            time.sleep(1.5 + 2 * attempt)
    return {}


def verdict(rec_t, rec_s, real):
    """对账：返回 'ok' / 'miss-title' / 'miss-singer' / 'unknown'"""
    if not real or not real.get("t"):
        return "unknown"
    def flat(x):
        # 多歌手写法差异（"A /B" vs "A/B"）不该判成串歌 → 连空格一起抹掉再比
        return E.norm(x or "").replace(" ", "")

    bt = flat(rec_t)
    ct = flat(real["t"])
    if not bt or not ct:
        return "unknown"
    if not (ct == bt or ct in bt or bt in ct):
        return "miss-title"
    # ★ 歌手**不能**走 E.strip()：strip 会连括号里的别名一起删掉
    #   （"冯沁苑(买辣椒也用券)" → "冯沁苑"），于是把「起风了/买辣椒也用券」
    #   这种**完全正确**的源误判成串歌。这里只做 norm（去标点但留内容）。
    ba = flat(rec_s)
    ca = flat(real.get("s") or "")
    if ba and ca and not (ca == ba or ca in ba or ba in ca):
        # 多歌手：逐个姓名比对，任一命中即算同一个人（"周深/毛不易" vs "毛不易/周深"）
        hit = False
        for one in filter(None, ba.replace("/", " ").split(" ")):
            if len(one) >= 2 and one in ca:
                hit = True
                break
        for one in filter(None, ca.replace("/", " ").split(" ")):
            if len(one) >= 2 and one in ba:
                hit = True
                break
        if not hit:
            return "miss-singer"
    return "ok"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry", action="store_true")
    ap.add_argument("--fix", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    a = ap.parse_args()

    st = json.load(open(STREAM, encoding="utf-8"))
    omap = st.get("map") or {}
    cur = E.curated()
    pairs = []                                    # (key, t, s, id)
    for k, v in omap.items():
        if not isinstance(v, dict):
            continue
        sid = v.get("wy")
        if not sid:
            continue
        c = cur.get(k)
        if not c:
            continue                              # 不在可见曲目里，无从对账
        pairs.append((k, c.get("t") or "", c.get("s") or "", str(sid)))
    if a.limit:
        pairs = pairs[:a.limit]
    print("待体检 %d 条（有 wy 源且能定位到曲目名）" % len(pairs), flush=True)

    st_c = {}
    bad_keys, unknown = [], []
    t0 = time.time()
    for i in range(0, len(pairs), BATCH):
        chunk = pairs[i:i + BATCH]
        got = detail([p[3] for p in chunk])
        for k, t, s, sid in chunk:
            r = got.get(sid)
            vd = verdict(t, s, r)
            st_c[vd] = st_c.get(vd, 0) + 1
            if vd == "unknown":
                unknown.append(k)
            elif vd != "ok":
                bad_keys.append((k, vd, (r or {}).get("t"), (r or {}).get("s"), t, s))
        if (i // BATCH) % 5 == 0:
            print("  %d/%d ｜%ds ｜串歌 %d" % (i + len(chunk), len(pairs),
                                              int(time.time() - t0), len(bad_keys)), flush=True)
        time.sleep(0.25)

    print("\n=== 体检结论 ===")
    for k2, n in sorted(st_c.items(), key=lambda x: -x[1]):
        print("  %-12s %d" % (k2, n))
    print("\n--- 串歌样例（前 20：想要 / 实际）---")
    for k, vd, rt, rs, wt, ws in bad_keys[:20]:
        print("  [%s] 要「%s / %s」却是「%s / %s」" % (vd, wt, ws, rt, rs))

    if a.fix and bad_keys:
        removed = 0
        for k, *_ in bad_keys:
            if k in omap:
                omap.pop(k, None)
                removed += 1
        st["map"] = omap
        st["hit"] = len(omap)
        cur_n = len(cur)
        st["rate"] = round(100.0 * len(omap) / max(1, cur_n), 1)
        json.dump(st, open(STREAM, "w", encoding="utf-8"), ensure_ascii=False)
        print("\n🧹 已清除串歌条目 %d 条 → 剩余 %d 条（%.1f%%）"
              % (removed, len(omap), st["rate"]))
    elif a.dry:
        print("\n--dry：不写盘")
    elif bad_keys:
        print("\n（未加 --fix，未写盘）")


if __name__ == "__main__":
    main()
