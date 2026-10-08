#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""曲库体检器 —— 发布前的机器闸门（不合格直接退非零，阻断入库）

为什么必须有：2026-10-05 曲库混入 31% 翻唱垃圾、2026-10-06 又发现线上 13 万首里
61% 是 pop=5~9 的有声书/播客/白噪音/素材库。人眼抽查看不出量级问题，机器能。

检查项：
  A 结构：manifest 与实际分片一致、id 唯一、必需字段齐全、索引行数对得上
  B 可播：不含 VIP/付费(fee∈{1,4})、不含无版权下架(nrc=1)、时长在 30s~15min
  C 干净：歌名/歌手不命中垃圾与非歌曲词表
  D 成色：热度分位达标（p50 ≥ MIN_P50、热门占比 ≥ MIN_HOT_PCT）——这条专防「垃圾回灌」
用法：python tools/catalog_verify.py [catalog 目录，默认 data/catalog]
"""
import json, os, re, sys, glob, collections

CAT = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "catalog")

SKIP_FEE = {1, 4}
MIN_DUR, MAX_DUR = 30000, 900000
MIN_P50_POP = 10          # 中位热度下限（防垃圾回灌；实测干净库 p50≈35~85）
MIN_HOT_PCT = 20          # 热门(pop≥40)占比下限 %
JUNK_RE = re.compile(
    r"翻唱|cover|伴奏|instrumental|抖音|铃声|纯音乐|钢琴版|吉他版|尤克里里|八音盒|"
    r"口琴|陶笛|葫芦丝|萨克斯|二胡|古筝|电子琴|哼唱|清唱|翻自|ktv|慢速|加速|降调|升调|"
    r"remix|八轨|和声版|消音|立体声环绕|睡眠|白噪音|胎教|助眠|asmr|钢琴曲|轻音乐|"
    r"洞箫|笛子版|琵琶版|箫版|筝版|埙|笙版|唢呐|扬琴|马头琴|手风琴|"
    r"audiobook|audio\s*book|bookstream|朗读|有声书|有声剧|播客|podcast|广播剧|"
    r"chapter\s*\d|kapitel\s*\d|teil\s*\d|episode\s*\d|电台剧|朗读版|"
    r"nature\s*sound|yoga|meditation|spa\s*music|white\s*noise|rain\s*sound|"
    r"素材|production\s*music|trailer\s*music|背景音乐|彩铃", re.I)
# dj 收紧版（与 harvest.py 同口径）：只认中文名尾部，放过 Dj Snake 这类正版艺人
JUNK_DJ_RE = re.compile(r"[\u4e00-\u9fa5]\s*dj\s*(?:版|mix|remix)?\s*$", re.I)


def is_junk(*fields):
    for s in fields:
        s = s or ""
        if JUNK_RE.search(s) or JUNK_DJ_RE.search(s):
            return True
    return False

bad = []
warn = []


def check(ok, msg):
    (print if ok else (lambda m: (bad.append(m), print(m))[1]))(("  ✓ " if ok else "  ✗ ") + msg)


man = json.load(open(os.path.join(CAT, "manifest.json"), encoding="utf-8"))
print("体检曲库：%s" % CAT)
print("manifest：count=%s 分片=%s 更新时间=%s"
      % (man.get("count"), len(man.get("shards") or []), man.get("updated")))

# ---------- A 结构 ----------
arr, ids = [], set()
files = set(os.path.basename(p) for p in glob.glob(os.path.join(CAT, "shard-*.json")))
miss = [s["file"] for s in man.get("shards") or [] if s["file"] not in files]
check(not miss, "分片文件齐全（缺 %d 个）" % len(miss))
dupe_struct = 0
for s in man.get("shards") or []:
    chunk = json.load(open(os.path.join(CAT, s["file"]), encoding="utf-8"))
    if len(chunk) != s.get("count"):
        dupe_struct += 1
    arr += chunk
check(dupe_struct == 0, "每个分片曲目数与 manifest 声明一致")
check(len(arr) == (man.get("count") or 0),
      "分片合计 %d == manifest %s" % (len(arr), man.get("count")))
for x in arr:
    if x.get("i") in ids:
        bad.append("重复曲目 id %s" % x.get("i"))
    ids.add(x.get("i"))
check(len(ids) == len(arr), "曲目 id 唯一（%d 个）" % len(ids))

# ★★ 2026-10-08 必需字段收紧为「端上非有不可」的三项：
#   i(id) / n(歌名) / a(歌手) 缺 → 端上要么播不出、要么显示空 → 必须阻断；
#   p(封面) 缺 → 端上 assetURL 不命中会自动回落上游 URL、再不行用占位图 ph() 生成文字块，
#   不影响可用性。旧版把 p 也列为必需 → 29 首「只是封面抓取失败」的好歌
#   把整批 79,457 首**全盘阻断**（run 37736589880 实测，线上停在旧的 68,903 首）。
#   现在：p 缺失降为「警告」，只报数、不拦发布。
CRIT = ("i", "n", "a")
lack = sum(1 for x in arr if any(not x.get(k) for k in CRIT))
check(lack == 0, "必需字段齐全（i/n/a 缺 %d 条）" % lack)
_no_p = sum(1 for x in arr if not x.get("p"))
if _no_p:
    warn.append("封面(p)缺失 %d 条（端上回落上游 URL + ph() 占位，不阻断发布）" % _no_p)
print("  %s 封面(p)齐全（缺 %d 条，不阻断）" % ("✓" if _no_p == 0 else "！", _no_p))

idx = man.get("idx") or {}
idx_files = idx.get("chunks") or []
idx_missing = [f for f in idx_files if not os.path.exists(os.path.join(CAT, f))]
check(not idx_missing, "搜索索引分块齐全（缺 %d 个）" % len(idx_missing))
lines = 0
for f in idx_files:
    with open(os.path.join(CAT, f), encoding="utf-8") as fh:
        lines += sum(1 for _ in fh)
check(lines == len(arr), "索引行数 %d == 曲目数 %d" % (lines, len(arr)))
dangling = 0
seps = 0
for f in idx_files[:1]:
    for ln in open(os.path.join(CAT, f), encoding="utf-8"):
        parts = ln.rstrip("\n").split(idx.get("sep") or "\u0001")
        if len(parts) < 6:
            seps += 1
        else:
            try:
                si = int(parts[3])
                if not (0 <= si < len(man.get("shards") or [])):
                    dangling += 1
            except Exception:
                dangling += 1
check(seps == 0 and dangling == 0, "索引首块格式正确（异常 %d 行）" % (seps + dangling))

# ---------- A2 端上消费层（js 包）必须与 manifest 同代 ----------
#   ★ 为什么单列这一项（2026-10-07 真事故）：
#     端上 app.html 读的**不是 manifest.json，而是 data/catalog/js/cat-manifest.js**
#     （file:// 下零网络优先读包内；且 refresh() 只在 CDN 那份 updated
#     **与包内不同**时才切到 CDN —— 两份同代 = 永远不切 = 手机永远旧库）。
#     而 harvest 里 _sync_js() 的异常是「只告警不抛出」→ js 包静默停在上一代时，
#     上面 A/B/C/D 全部照样「✓ 通过」→ 曲库看着发布成功、手机却看不到。
#     本项把这个静默失败变成红灯（宁可这轮不发，也不让线上数据与端上消费层分裂）。
JS = os.path.join(CAT, "js")
_mjs = None
_mp = os.path.join(JS, "cat-manifest.js")
if not os.path.exists(_mp):
    check(False, "端上曲库包 cat-manifest.js 存在（缺文件）")
else:
    _s = open(_mp, encoding="utf-8").read()
    _k = "window.__CATMAN="
    try:
        _mjs = json.loads(_s[_s.index(_k) + len(_k):].strip().rstrip(";").strip())
    except Exception as e:
        check(False, "端上曲库包 cat-manifest.js 可解析（%r）" % (e,))
if _mjs is not None:
    check(_mjs.get("count") == man.get("count"),
          "端上清单曲目数 %s == manifest %s" % (_mjs.get("count"), man.get("count")))
    check(_mjs.get("updated") == man.get("updated"),
          "端上清单代次 updated=%s == %s" % (_mjs.get("updated"), man.get("updated")))
    _nsh = len(_mjs.get("shards") or [])
    check(_nsh == len(man.get("shards") or []),
          "端上清单分片数 %d == %d" % (_nsh, len(man.get("shards") or [])))
    # 端上按 Math.floor(i/SH_PACK=10) 取 cat-sh-NN.js；索引按 cat-idx-NN.js
    _packs = ["cat-sh-%02d.js" % g for g in range((_nsh + 9) // 10)]
    _ipacks = ["cat-idx-%02d.js" % i
               for i in range(len((_mjs.get("idx") or {}).get("chunks") or []))]
    _lack = [f for f in _packs + _ipacks if not os.path.exists(os.path.join(JS, f))]
    check(not _lack, "端上分块包齐全（缺 %d 个：%s）" % (len(_lack), ",".join(_lack[:5])))
    # 分块内曲目数合计必须等于清单（防「清单已换、分块还是上代」）
    # 解析口径：mkcatalog_js 产出形如 window.__CATSH[n]=<json>; 且 json 为紧凑单行
    #   → 用 ';window.__CATSH[' 切段、每段首个 ']=' 即边界。任何解析异常一律判失败（fail-closed）。
    _tot = 0
    for f in [p for p in _packs if os.path.exists(os.path.join(JS, p))]:
        for piece in open(os.path.join(JS, f), encoding="utf-8").read().split(";window.__CATSH[")[1:]:
            try:
                _tot += len(json.loads(piece.split("]=", 1)[1].rstrip().rstrip(";").strip()))
            except Exception:
                _tot = -1
                break
        if _tot < 0:
            break
    check(_tot == (man.get("count") or 0),
          "端上分块曲目数合计 %s == manifest %s" % (_tot, man.get("count")))
    # 索引分块必须与 idx-NN.txt 逐字节一致（端上搜索就是拿它做的）
    _same = True
    for i, c in enumerate((_mjs.get("idx") or {}).get("chunks") or []):
        try:
            _s2 = open(os.path.join(JS, "cat-idx-%02d.js" % i), encoding="utf-8").read()
            _got = json.loads(_s2.split("window.__CATIDX[", 1)[1].split("]=", 1)[1].rstrip().rstrip(";").strip())
            _same = _same and (_got == open(os.path.join(CAT, c), encoding="utf-8").read())
        except Exception:
            _same = False
    check(_same, "端上索引分块与 idx-*.txt 逐字节一致（%d 块）"
          % len((_mjs.get("idx") or {}).get("chunks") or []))

# ---------- B 可播 ----------
vip = sum(1 for x in arr if x.get("f") in SKIP_FEE)
nrc = sum(1 for x in arr if x.get("nrc"))
durbad = sum(1 for x in arr if not (MIN_DUR <= (x.get("d") or 0) <= MAX_DUR))
check(vip == 0, "无 VIP/付费曲目（%d 条）" % vip)
check(nrc == 0, "无版权下架曲目（%d 条）" % nrc)
check(durbad == 0, "时长均在 30s~15min（越界 %d 条）" % durbad)

# ---------- C 干净 ----------
_junkall = [x for x in arr if is_junk(x.get("n"), x.get("a"))]
junk = _junkall[:5]
check(not junk, "无翻唱/伴奏/有声书/白噪音等垃圾（命中 %d 条，例：%s）"
      % (len(_junkall),
         "；".join("%s—%s" % (x["n"][:16], x["a"][:12]) for x in junk[:2])))

# ---------- D 成色 ----------
pops = sorted(x.get("pop") or 0 for x in arr)
if pops:
    q = lambda p: pops[min(len(pops) - 1, int(len(pops) * p))]
    p50, hot = q(.5), sum(1 for p in pops if p >= 40)
    hot_pct = 100.0 * hot / len(pops)
    print("  成色：p10=%d p25=%d p50=%d p75=%d p90=%d ｜ 热门(pop≥40) %d = %.1f%% ｜ 歌手 %d 位"
          % (q(.1), q(.25), p50, q(.75), q(.9), hot, hot_pct, len({x["a"] for x in arr})))
    check(p50 >= MIN_P50_POP, "中位热度 p50=%d ≥ %d" % (p50, MIN_P50_POP))
    check(hot_pct >= MIN_HOT_PCT, "热门占比 %.1f%% ≥ %d%%" % (hot_pct, MIN_HOT_PCT))
    if hot_pct < 50:
        warn.append("热门占比偏低（%.1f%%）：库偏长尾" % hot_pct)

# ---------- E 音频可播（抽样实测，2026-10-06 追加）----------
# 为什么抽样而不全量：全量 HEAD 13 万首要 ~18 分钟，而这一步的目标是「防回归」，
# 抽 240 首（95% 置信度下能发现 >1.2% 的劣化）已足够。
# 判据与 harvest.audio_gate 完全一致：字节数 + 反算码率。
# 背景：fee=0 的周杰伦《稻香》实测回 4515 字节 HTML；VIP 试听片段伪装成 audio/mpeg
# （泪桥 481115B/225s = 17kbps），只有 Content-Length 能识别。
if "--no-audio" not in sys.argv:
    import ssl, urllib.request, urllib.error
    _ctx = ssl.create_default_context(); _ctx.check_hostname = False; _ctx.verify_mode = ssl.CERT_NONE
    _h = {"User-Agent": ("Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) "
                         "AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148"),
          "Referer": "https://music.163.com/"}
    import random
    from concurrent.futures import ThreadPoolExecutor
    random.seed(20261006)
    _samp = random.sample(arr, min(240, len(arr)))

    def _probe(x):
        try:
            r = urllib.request.Request(
                "https://music.163.com/song/media/outer/url?id=%d.mp3" % int(x["i"]),
                headers=_h, method="HEAD")
            with urllib.request.urlopen(r, timeout=10, context=_ctx) as resp:
                cl = int(resp.headers.get("Content-Length") or 0)
                ct = (resp.headers.get("Content-Type") or "").lower()
        except urllib.error.HTTPError:
            # ★★ 2026-10-08 与 harvest._outer_bytes 对齐（第二处血案）：
            #   4xx/5xx = 被拒 / 风控 / 下架，**不是**「这首是死链」。
            #   harvest 那边返回 (-1,"") → _verdict 判 unknown → **放行**；
            #   而旧版体检在这里读 `e.headers.get("Content-Length") or 0` → 0 < 20000 → **判死**。
            #   同一首歌「采集放行、体检判死」—— 闸门自相矛盾，harvest 被自己的体检卡死
            #   （run 37736589880 报的 5 条「死链」多半就是这一类）。
            return None                                  # 不可判 → 不计入分母
        except Exception:
            return None                                  # 网络失败不计入分母
        # ── 以下判据必须与 harvest._verdict 逐行同源 ─────────────────────────
        if cl == 0 and not ct:
            return None                                  # 不可信的 0 字节（风控产物）→ 放行
        if "text/html" in ct or "application/json" in ct:
            return ("dead", x)                           # 语义级：网易的「不可播」下载页
        if ct and not ct.startswith("audio/"):
            return None                                  # 非 audio 也非已知错误页 → 测不准，放行
        if cl < 20000:
            return ("dead", x)
        dur = (x.get("d") or 0) / 1000.0
        if dur <= 0:
            return ("keep", x)
        kb = cl * 8 / dur / 1000.0
        if kb > 450:
            return ("keep", x)          # 异常高码率（无损/多轨）不受下界约束
        # ★★★ 2026-10-07 修正：判据必须与 harvest._verdict **完全对齐** ——
        #   判「试听片段」看**字节覆盖率**，不是码率下界。
        #   旧写法 `70 <= kb <= 450` 会把低码率正常内容整片误判成片段（老录音、纯人声、
        #   听力音频都在 32~64kbps），而这批内容 harvest 那边**已经用覆盖率判据放行过了**
        #   —— 体检拿更严的尺子再筛一遍，必然把合格曲目判成 clip，于是可播率被自己压低，
        #   harvest 被自己的体检误拦（2026-10-06 深夜 run 37542937388 就这么挂的）。
        #   harvest.py 第 411 行早有血案注释：「旧逻辑用 70kbps 卡，会把中间那类低码率
        #   正常歌整片误判成试听片段剔掉」——体检当时没跟上，现在补上。
        cover = (cl * 8 / 32 / 1000.0) / dur        # AUDIO_KBPS_LO = 32（与 harvest 同源同值）
        return (("keep" if cover >= 0.75 else "clip"), x)

    with ThreadPoolExecutor(24) as ex:
        _res = [r for r in ex.map(_probe, _samp) if r]
    _ok = sum(1 for k, _ in _res if k == "keep")
    _dead = [x for k, x in _res if k == "dead"]
    _clip = [x for k, x in _res if k == "clip"]
    if _res:
        _rate = 100.0 * _ok / len(_res)
        check(_rate >= 98.0,
              "音频可播率 %.1f%% ≥ 98%%（抽 %d 首实测：死链 %d、片段 %d）%s"
              % (_rate, len(_res), len(_dead), len(_clip),
                 "" if _rate >= 98 else "｜例：" + "；".join(
                     "%s—%s" % (x["n"][:14], x["a"][:10]) for x in (_dead + _clip)[:2])))

print("\n结论：%s" % ("❌ 体检不通过，禁止发布（%d 项）" % len(bad) if bad else "✅ 体检通过"))
for w in warn:
    print("  ⚠ " + w)
sys.exit(1 if bad else 0)
