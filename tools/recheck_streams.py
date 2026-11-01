# -*- coding: utf-8 -*-
r"""★ 音源表「国内直连」全量复检 —— 把"入库时能播、用户手机上放不出来"的坏链揪出来。

============================= 为什么需要这一步 =============================
2026-11-01 补完源后服务端统计可播率 92.3%（4718/5105），但端上真测抽样只有
**69/90（76.7%）**：17 首稳定返回 `text/html`（VIP / 版权 / 下架的错误页）。

差距的来源（已逐个排除）：
  · 不是并发限流 —— 并发 3 与并发 8 结果完全一致（都是 69/90）；
  · 不是地域差异 —— 抽 3 个失败 id 在国内直连 / 海外代理下**都**回 206 audio/mpeg；
  · 结论：这批 id 就是**真放不出来**（网易对未登录/非 VIP 回 HTML 错误页），
    而服务端补源 probe 时恰好拿到了能通的响应（网易外链状态会随时间/节点波动）。

留着这些坏 id 的代价比"没有"更大：端上会拿它去试 → 等到超时才报错 →
用户看到的就是"点了半天没反应"。标成 dead 之后端上**直接跳过**，
立刻走在线补源（XFIND）去找别的线路，体验反而更快。

============================= 怎么做 =============================
① 对 data/streams.json 里每一条（可选 --limit）用**国内直连**发 Range GET；
② 通过：Content-Type 是 audio/video，或文件头是 ID3 / MP3 帧同步 / ftyp / RIFF / fLaC / OggS；
③ 失败：打 `dead: 1` 标记并记 data/streams_dead.json（含失败原因），
   连续 `--max-dead` 轮都失败就从 map 里彻底移除（默认 3 轮，先标不改）；
④ 已标 dead 的条目下次仍会复检（状态会波动），连续通过 `--clear` 轮就摘掉标记。

用法：
    python tools/recheck_streams.py --dry            # 只报数
    python tools/recheck_streams.py --workers 6      # 实跑
    python tools/recheck_streams.py --limit 300      # 抽样试跑
"""
import argparse
import concurrent.futures as cf
import json
import os
import sys
import time
import urllib.request

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
STREAM = os.path.join(ROOT, "data", "streams.json")
DEADF = os.path.join(ROOT, "data", "streams_dead.json")

UA = {"User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
                    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 "
                    "Mobile/15E148 Safari/604.1"}
# ★ 关键：复检必须走**国内直连**（用户手机的网络环境）。
#   走代理会以海外身份探测，把国内放不出来的 VIP 曲误判成可播 —— 那就白检了。
OP = urllib.request.build_opener(urllib.request.ProxyHandler({}))

OUTER = "https://music.163.com/song/media/outer/url?id=%s.mp3"
KW = "http://antiserver.kuwo.cn/anti.s?type=convert_url&rid=MUSIC_%s&format=mp3&response=url"


def url_of(rec):
    """按记录里实际那条线路复检（wy 用外链，kw 用酷我兑换链）"""
    if rec.get("wy"):
        return OUTER % rec["wy"]
    if rec.get("kw"):
        return KW % rec["kw"]
    return None


def kw_real(rid):
    r"""酷我「兑换链」两段式：anti.s 返回的**不是音频**，是一段 URL 文本。

    ★ 2026-11-02 实测翻案（这是本文件此前最大的误杀）：
       旧代码拿 anti.s 直接当音频探测 → Content-Type: text/plain → 判死，
       于是 **312 条 kw 源被打成 312/312 全灭**（连官方 `srcs` 统计里 kw 都整片 dead）。
       实际抽样 25 条：先兑换、再取真实地址，**25/25 全是 audio/mpeg 真 MP3（100%）**。
       —— 不是版权问题，是复检方法错了。不先兑换就是白杀 312 首。

    返回真实音频 URL（http 一律升 https：iOS ATS 拒绝明文，实测 https 域名同样可下）。
    """
    try:
        t = OP.open(urllib.request.Request(KW % rid, headers=UA),
                    timeout=12).read().decode("utf-8", "replace").strip()
    except Exception:
        return None
    if not t.startswith("http"):
        return None                                  # 兑换失败/下架
    if t.startswith("http://"):
        t = "https://" + t[len("http://"):]
    return t


def probe(u):
    try:
        req = urllib.request.Request(u, headers=dict(UA, Range="bytes=0-2047"))
        r = OP.open(req, timeout=20)
        ct = (r.headers.get("Content-Type") or "").lower()
        head = r.read(64)
        if ct.startswith("text/"):
            return False, "html:" + ct[:16]
        if ct.startswith("audio/") or ct.startswith("video/"):
            return True, "ok"
        if head[:3] == b"ID3":
            return True, "id3"
        if len(head) >= 2 and head[0] == 0xFF and (head[1] & 0xE0) == 0xE0:
            return True, "mp3sync"
        if head[4:8] == b"ftyp" or head[:4] in (b"RIFF", b"fLaC", b"OggS"):
            return True, "magic"
        return False, "ctype:" + ct[:16]
    except Exception as e:
        return False, "err:" + repr(e)[:32]


def check(rec):
    # ① 酷我：必须先兑换出真实地址再测（直接测 anti.s 只会拿到一段文本）
    if rec.get("kw"):
        real = kw_real(rec["kw"])
        if not real:
            return False, "kw-nourl"
        return probe(real)
    # ② 咪咕：播放地址接口（MIGUM2.0 strategy/listen-url 与 MIGUM3.0 sub/listenV2）
    #    2026-11-02 实测一律回 `code:299996 路由请求不支持`（换 host / 加 channel /
    #    ua / toneFlag 头都不行）→ 这是接口级失效，不是单曲版权，没救。
    if rec.get("migu") or rec.get("mg"):
        return False, "migu-api-dead"
    u = url_of(rec)
    if not u:
        return False, "no-url"
    return probe(u)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry", action="store_true")
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--max-dead", type=int, default=3,
                    help="连续失败这么多轮就从 map 里移除（默认 3）")
    a = ap.parse_args()

    st = json.load(open(STREAM, encoding="utf-8"))
    omap = st.get("map") or {}
    dead = {}
    if os.path.exists(DEADF):
        try:
            dead = json.load(open(DEADF, encoding="utf-8"))
        except Exception:
            dead = {}

    # 咪咕线路已接口级失效（check() 里 299996），留着只会让端上白等 —— 直接清掉，
    # 不等「连续 3 轮」慢慢淘汰。清掉后这些歌回到「无源」状态，由补源流程重新找线路。
    mg = [k for k, v in omap.items()
          if isinstance(v, dict) and (v.get("migu") or v.get("mg")) and not v.get("wy")]
    for k in mg:
        omap.pop(k, None)
        dead.pop(k, None)
    if mg:
        print("咪咕线路接口级失效（299996）→ 直接清除 %d 条" % len(mg), flush=True)

    keys = list(omap.keys())
    if a.limit:
        keys = keys[:a.limit]
    print("复检 %d 条（map 共 %d 条）" % (len(keys), len(omap)), flush=True)

    t0 = time.time()
    bad, good = {}, 0
    with cf.ThreadPoolExecutor(max_workers=a.workers) as ex:
        futs = {ex.submit(check, omap[k]): k for k in keys}
        for n, fu in enumerate(cf.as_completed(futs), 1):
            k = futs[fu]
            try:
                ok, why = fu.result()
            except Exception as e:
                ok, why = False, "err:" + repr(e)[:32]
            if ok:
                good += 1
                if k in dead:
                    dead.pop(k, None)          # 恢复了 → 摘标记
                    if k in omap:
                        omap[k].pop("dead", None)
            else:
                bad[k] = why
                if k in omap:
                    omap[k]["dead"] = 1
                dead[k] = int(dead.get(k) or 0) + 1
            if n % 500 == 0:
                print("  %d/%d ｜%.0fs ｜通过 %d ｜失败 %d"
                      % (n, len(keys), time.time() - t0, good, len(bad)), flush=True)

    print("\n=== 复检结论 ===")
    print("  真能播 %d / %d（%.1f%%）" % (good, len(keys), 100.0 * good / max(1, len(keys))))
    print("  放不出来 %d 条" % len(bad))
    for w, n in sorted(((w, list(bad.values()).count(w)) for w in set(bad.values())),
                       key=lambda x: -x[1])[:6]:
        print("      %-26s %d" % (w, n))

    # 连续多轮都失败 → 彻底移除，别再让端上白等
    doomed = [k for k in bad if int(dead.get(k) or 0) >= a.max_dead]
    if doomed:
        print("  连续 ≥%d 轮失败 → 本轮移除 %d 条" % (a.max_dead, len(doomed)))

    if a.dry:
        print("--dry：不写盘")
        return 0

    for k in doomed:
        omap.pop(k, None)
        dead.pop(k, None)

    cur_keys = set(omap.keys())
    dead = {k: v for k, v in dead.items() if k in cur_keys}

    srcs = {}
    for v in omap.values():
        s = v.get("src") or ("wy" if v.get("wy") else ("kw" if v.get("kw") else "?"))
        srcs[s] = srcs.get(s, 0) + 1
    st["map"] = omap
    st["srcs"] = srcs
    st["hit"] = len([k for k in omap if not omap[k].get("dead")])
    st["count"] = st.get("count") or len(omap)
    st["rate"] = round(100.0 * st["hit"] / max(1, st["count"]), 1)
    st["rechecked"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    st["note"] = (st.get("note") or "") + (
        " ★ 2026-11-01 起 tools/recheck_streams.py 按**国内直连**全量复检，"
        "放不出来的打 dead 标记（端上跳过，不再干等超时），连续 %d 轮失败则移除。" % a.max_dead)
    json.dump(st, open(STREAM, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    json.dump(dead, open(DEADF, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print("💾 streams.json：可播 %d 条（%.1f%%）｜移除 %d 条"
          % (st["hit"], st["rate"], len(doomed)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
