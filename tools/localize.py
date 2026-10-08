#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
localize.py —— 图片本地化沉淀（把「别人的 URL」变成「自己的文件」）

★★ 为什么必须做（2026-10-06 主人点破）：
   「你要知道采集器抓回来的东西有没有变成自己的东西，有没有沉淀」
   体检发现：歌手头像 / 首页 KV / 歌库封面 **全都只是存了别人的 URL**
     · 头像  https://y.gtimg.cn/music/photo_new/T001R300x300M000<mid>.jpg   ← QQ 的服务器
     · KV    https://y.gtimg.cn/music/photo_new/T002R800x800M000<albummid>.jpg
     · MV 封面 http://p1.music.126.net/...                                  ← 网易云的服务器
   上游一改域名 / 加防盗链，三处**同时白屏**，App 就成空壳。

   本模块做的事：把图片**下载下来 → 转 WebP（体积≈JPEG 1/3）→ 落盘 data/assets/**，
   并生成自己的索引 `assets/<kind>.json`（文件名 = 内容 sha1 前 16 位）。
   App 端改为读「自己的静态资源」，上游怎么改都与我无关 —— 这才是沉淀。

产物：
  data/assets/av/<hash>.webp            歌手头像（128×128，约 4~6 KB/张）
  data/assets/kv/<hash>.webp            首页 KV 海报（720×720，约 30~50 KB/张）
  data/assets/cv/<hash>.webp            歌库/MV 封面（400×400，约 15~25 KB/张）
  data/assets/<kind>.json               索引：{原名/键: 文件相对路径}
  data/assets/manifest.json             总账：各栏目成功/失败/总字节

★★ 体量分层（2026-10-06 实测核算，必须遵守，否则仓库存不下）：
   曲库 101256 首 → 唯一封面 **70987 张**（同专辑多曲共用，复用率 1:1.43）。
   WebP 实测均值 **14.9 KB/张**：
     热度前  5000 首 →  4449 张 ≈  66 MB
     热度前 20000 首 → 17024 张 ≈ 254 MB
     热度前 40000 首 → 33306 张 ≈ 496 MB
     全部   101256 首 → 70987 张 ≈ 1030 MB   ← 超 GitHub 1GB 仓库软限，**不能全下**

   ★★ 为什么**不走 Releases 当主力**（我实测后修正的方案，2026-10-06）：
     GitHub Releases 附件虽然"单文件 2GB、无总量上限、免费带宽"，但**实测下载
     只有 228 B/s ~ 4.6 KB/s**（本机网络环境下），端上根本刷不出图。
     实测对比：
        gcore.jsdelivr.net   22 KB/s  ✅ 唯一可用
        cdn.jsdelivr.net     1.4 KB/s  ❌ 被回源墙
        raw.githubusercontent 超时      ❌ 不通
        Releases 直链       0.2~4.6KB/s ❌ 太慢
     → **热图必须走 gcore.jsdelivr（即 git 仓库）**，Releases 只作防丢冷备。

   策略：**按热度分层增量**（不是拍脑袋的死数）：
     · 每轮由 CV_LIB_TOP 控制本轮覆盖到第几名（默认 40000 首，约 496MB，
       压在 asset_guard 的 700MB 安全线内）；
     · 只保热度高的、冷门长尾交给端上兜底（上游 URL 永远留在数据里）；
     · 这层取舍是**必然的**：要么仓库爆，要么长尾慢 —— 选后者，因为长尾极少被点。
"""
import io
import json
import os
import re
import sys
import time
import urllib.request
import urllib.error
import ssl
import hashlib
from concurrent.futures import ThreadPoolExecutor, as_completed

try:
    from PIL import Image
    _HAS_PIL = True
    # ★ 实测踩到：网易云有少量**超大图**（单张 1.7 亿像素，如某张专辑封面原图），
    #   Pillow 默认 8900 万像素即抛 DecompressionBombWarning 并在极端情况拒绝解码。
    #   我们本来就只取 400×400，超大图纯属浪费内存 —— 直接放开阈值，
    #   解码后立刻 crop+resize，不会真的把 1.7 亿像素留在内存里。
    Image.MAX_IMAGE_PIXELS = None
except Exception:
    _HAS_PIL = False

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data")
AS = os.path.join(DATA, "assets")

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36")
CTX = ssl.create_default_context()
CTX.check_hostname = False
CTX.verify_mode = ssl.CERT_NONE

# 每种图的目标尺寸与质量（WebP）
#   max = 该类的**总量硬上限**（超了就只保热门，冷门不再新增；已下的不清）
#   ★★ 2026-10-08 语义纠正：max 是「**落盘唯一图文件数**」上限，**不是索引键数**。
#     血案：索引里一张图会有多个键（`u:<sha1(url)>` 主键 + `skey(歌名|歌手)` 端上命中键
#     + `mv:<id>`），键数 ≈ 图数 × 2.2。旧门控把「键数」当容量计数 →
#     cv 键数涨到 46,177 后 > max 26,000 → 门控恒真 → **每轮 new=0，封面本地化在 CI 里永久冻结**
#     （实测 10-08：`count=46177 new=0 cached=14863 deferred=36266`，3.6 万条封面永远排队）。
#     现在门控改用 len(set(old.values())) 计数（见 run_kind），max 也随之按「图数」定标。
#   cv 定标依据（2026-10-08 实测）：均值 **16.6 KB/张**（改后实测 25086 张 / 405.7MB 反推，
#     旧值 16.27 偏小 2%；第 27 行那个 14.9 是 10-06 的粗估，偏小 11%，勿再用于新估算）
#     → 36000 张 ≈ 584MB；加 av/kv 与索引后 data/assets ≈ 592MB，
#     压在 asset_guard 的 700MB 安全线内（改后实测已落 25086 张 / 414.7MB，余量约 190MB）。
SPEC = {
    "av": {"dir": "av", "size": 128, "q": 82, "max": 4000},      # 歌手头像（小、量大）
    "kv": {"dir": "kv", "size": 720, "q": 80, "max": 400},       # 首页海报（大图、量小）
    "cv": {"dir": "cv", "size": 400, "q": 78, "max": 36000},     # 歌库/MV/分类封面（中、量最大）
}

WORKERS = int(os.environ.get("LZ_WORKERS") or "10")
TIMEOUT = int(os.environ.get("LZ_TIMEOUT") or "20")
# ★ 曲库封面的热度分层：本轮取「热度前 N 首」的封面（0=不取曲库封面）
#   默认 40000（约 496MB），压在 asset_guard 700MB 安全线内；
#   实测曲库热度分布：pop>=90 有 30818 首(30.4%)、pop>=70 有 59297 首(58.6%)。
CV_LIB_TOP = int(os.environ.get("CV_LIB_TOP") or "40000")


def log(*a):
    print(*a, flush=True)


def _get(url):
    """下载原图（返回 bytes 或 None）

    ★ Referer 按域名自适应：实测上游图床有防盗链 ——
       · y.gtimg.cn（QQ 音乐）→ 需要 https://y.qq.com/
       · p1/p2.music.126.net（网易云）→ 需要 https://music.163.com/
      统一给 y.qq.com 时网易云会 403，故按域名分流。
    """
    if not url or not url.startswith("http"):
        return None
    host = url.split("/")[2] if "//" in url else ""
    if "126.net" in host or "music.163" in host:
        ref = "https://music.163.com/"
    elif "gtimg" in host or "qq.com" in host:
        ref = "https://y.qq.com/"
    else:
        ref = "https://music.163.com/"
    h = {"User-Agent": UA, "Accept": "image/*,*/*;q=0.8",
         "Referer": ref, "Accept-Language": "zh-CN,zh;q=0.9"}
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=h),
                                    timeout=TIMEOUT, context=CTX) as r:
            if r.status != 200:
                return None
            # 原图上限 8MB，防跑飞。★ 必须做截断检测：
            #   read(N) 返回满 N 说明后面还有数据没读完，PIL 会解出一张**半截图**
            #   （早期只写 read(3MB) 且不判断，遇到大图会静默存成坏图）。
            #   宁可这一张不存，也不能让端上显示烂图 —— 端上没命中会回落远程 URL。
            b = r.read(8 * 1024 * 1024)
            if len(b) >= 8 * 1024 * 1024:
                return None
            return b if len(b) > 512 else None
    except Exception:
        return None


def _to_webp(raw, size, q):
    """JPEG/PNG → 居中裁方 → 缩放 → WebP。失败返回 None（调用方降级存原图）"""
    if not _HAS_PIL or not raw:
        return None
    try:
        im = Image.open(io.BytesIO(raw))
        # ★ JPEG 专用提速：让解码器直接按 1/2、1/4、1/8 降采样解码，
        #   而不是把整张原图（实测踩到过 1.7 亿像素）解进内存再缩。
        #   网易云封面绝大多数是 JPEG，这一条能把单张耗时从秒级压到毫秒级，
        #   也顺带把 16 线程并发时的内存峰值从 1.4GB 压下来。
        try:
            im.draft("RGB", (size * 2, size * 2))
        except Exception:
            pass
        im.load()
        # 转 RGB（WebP 不要 alpha 省体积；有 alpha 的贴白底）
        if im.mode in ("RGBA", "LA", "P"):
            bg = Image.new("RGB", im.size, (255, 255, 255))
            im = im.convert("RGBA")
            bg.paste(im, mask=im.split()[-1])
            im = bg
        elif im.mode != "RGB":
            im = im.convert("RGB")
        # 居中裁正方形（头像/封面要方，海报已方）
        w, h = im.size
        m = min(w, h)
        im = im.crop(((w - m) // 2, (h - m) // 2, (w + m) // 2, (h + m) // 2))
        if m > size:
            im = im.resize((size, size), Image.LANCZOS)
        out = io.BytesIO()
        im.save(out, "WEBP", quality=q, method=5)
        return out.getvalue()
    except Exception:
        return None


def _sha(raw):
    return hashlib.sha1(raw).hexdigest()[:16]


def _save(kind, raw):
    """把一张原图落盘成自己的 WebP，返回 (relpath, bytes) 或 (None, 0)"""
    sp = SPEC[kind]
    d = os.path.join(AS, sp["dir"])
    os.makedirs(d, exist_ok=True)
    webp = _to_webp(raw, sp["size"], sp["q"])
    if webp is None:
        return None, 0
    name = _sha(webp) + ".webp"
    p = os.path.join(d, name)
    if not os.path.exists(p):
        with open(p, "wb") as fh:
            fh.write(webp)
    return "%s/%s" % (sp["dir"], name), len(webp)


def _write_js(kind, body):
    """把索引另存一份 .js：`window.__DWG_ASSETS_<KIND>={<索引>};`

    ★ 为什么非要有 JS 版：iOS / Android 内核跑在 file:// 下，WebView **拦 fetch/XHR、
      不拦 <script> 子资源**（内核 app.html 曲库一节已写明这个坑，cat-*.js 就是这么绕的）。
      图片索引若只给 .json，端上 fetch 永远拿不到 → 图下好了却认不出文件名 →
      封面全部回落上游 URL —— 这就是「沉淀白做」的真正根因。
    """
    js_path = os.path.join(AS, kind + ".js")
    with open(js_path, "w", encoding="utf-8") as fh:
        fh.write("window.__DWG_ASSETS_%s=" % kind.upper())
        json.dump(body, fh, ensure_ascii=False, separators=(",", ":"))
        fh.write(";")
    return js_path


def _atomic_index(op, kind, allmap, new, cached, fail, deferred, note=None):
    """原子写索引（.json + .js 两份）。

    ★ 为什么必须原子：checkpoint 会在下载途中反复写同一个索引文件。
      直接覆盖写有窗口期——磁盘上会出现"半个 JSON"，端上/CI 读到就整个索引作废
      （现象是"图全在、一张都查不到"）。先写 .tmp 再 os.replace 是原子操作，
      任何时刻磁盘上的 op 要么是旧的完整版、要么是新的完整版。
    """
    body = {"updated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "kind": kind, "count": len(allmap), "new": new,
            "cached": cached, "fail": fail, "deferred": deferred,
            "note": note or "本地 WebP 静态资源；键→相对 data/assets 的路径。上游改域名与我无关。",
            "map": allmap}
    os.makedirs(AS, exist_ok=True)
    tmp = op + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(body, fh, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, op)
    _write_js(kind, body)
    return body


def run_kind(kind, mapping, keep_old=True):
    """mapping: {键: 远程URL}。返回 {键: 本地相对路径}（失败的键不出现）

    ★ 体量约束（仓库不能无限涨）：
      · 每轮新增上限 LZ_MAX_NEW（默认 1500 张/类）—— 攒量靠多轮，绝不一轮爆仓；
      · 总量上限 SPEC[kind]["max"]，超了就**先保热门**（mapping 的顺序由调用方按热度排）；
      · 单轮时间预算 LZ_BUDGET_S（默认 900s），超时优雅收尾（已下的照常落盘）。
    """
    sp = SPEC[kind]
    old = {}
    op = os.path.join(AS, kind + ".json")
    if keep_old and os.path.exists(op):
        try:
            old = json.load(open(op, encoding="utf-8")).get("map", {})
        except Exception:
            old = {}
    todo = {k: u for k, u in mapping.items() if u and u.startswith("http")}
    log("[%s] 待本地化 %d 张（已有 %d）" % (kind, len(todo), len(old)))

    ok, fail, total = {}, 0, 0
    t0 = time.time()
    cached = 0
    budget_s = int(os.environ.get("LZ_BUDGET_S") or "900")
    max_new = int(os.environ.get("LZ_MAX_NEW") or "1500")
    # ★★ 2026-10-06 加逃生阀：硬上限默认取 SPEC 里的 max，但允许用 LZ_HARD_CAP 临时抬高。
    #   为什么需要：曾因索引残缺导致 len(old) 偏小 → "len(old)+new >= hard_cap" 判断失效
    #   → 磁盘上堆了 36352 张图、索引里却只有 5473 条键（3 万+ 孤儿，占 600MB 却端上一张用不到）。
    #   恢复时把 cap 抬到 ≥ 磁盘图数，才能把这些**已经躺在磁盘上**的图重新认领回来。
    hard_cap = int(os.environ.get("LZ_HARD_CAP") or (sp.get("max") or 99999))
    # ★★ 2026-10-08 血案修复：容量计数必须按「**落盘唯一图文件数**」，绝不能按索引键数。
    #   一张图在索引里有多个键（`u:<sha1(url)>` 主键 + `skey(歌名|歌手)` 端上命中键 + `mv:<id>`），
    #   键数 ≈ 图数 × 2.2。旧写法 `len(old) + new >= hard_cap` 拿**键数**去比上限：
    #   实测 cv 键数涨到 46,177、max 却只有 26,000 → 条件恒真 → 每个新键都被 skip
    #   → `new` 永久为 0（现场：count=46177 new=0 cached=14863 deferred=36266）
    #   → 封面本地化在 CI 里**永久冻结**，3.6 万条封面永远排队。
    #   注意 `old` 的值就是相对路径（一张图可被多键共享），set() 后即真实图数。
    disk_files = len(set(old.values()))
    # ★ 防「索引残缺」低估容量：索引里没有的图（孤儿 / 上次硬中断留下的）**照样占磁盘**。
    #   以磁盘实际 .webp 文件数取大者，容量判断才不会被孤儿骗过去。
    #   （2026-10-06 血案：磁盘实有 36352 张、索引只认 5473 条 → 已吃掉 600MB 却还在"继续下"。）
    #   资产目录是扁平的（无子目录），listdir 足够且廉价。
    try:
        _real = sum(1 for _f in os.listdir(os.path.join(AS, sp["dir"])) if _f.endswith(".webp"))
    except Exception:
        _real = 0
    if _real > disk_files:
        log("[%s] 索引只认 %d 张、磁盘实有 %d 张 → 以磁盘为准（防孤儿吃容量）"
            % (kind, disk_files, _real))
        disk_files = _real
    log("[%s] 容量：落盘唯一图 %d 张 / 上限 %d 张（索引键 %d 条；本轮新增上限 %d）"
        % (kind, disk_files, hard_cap, len(old), max_new))
    skipped_budget = 0
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futs = {}
        seen_url = {}          # url → 本轮已为它排过任务的键（同 URL 只下一次）
        pend_alias = []        # (别名键, url)：同一 URL 已在别处排过，等主键下完事后回填
        for k, u in todo.items():
            # 已本地化过且文件还在 → 直接复用（增量，不重下）
            rel = old.get(k)
            if rel and os.path.exists(os.path.join(AS, rel)):
                ok[k] = rel
                cached += 1
                continue
            # ★★ 别名键按 URL 反查复用（跨轮次）
            #   同一个 URL 在索引里有多种键：u:<sha1(url)> 是「主键」，skey(歌名|歌手) 是端上
            #   命中用的「别名键」。别名键第一次出现时 old 里没有它 → 会重新下载+重新 WebP 编码
            #   同一张图。实测：4 万首分层有 3.9 万个 skey，其中绝大多数对应的图已经下过了，
            #   不反查就会把 CPU 全耗在重复编码上（进程 CPU 100%+ 而吞吐掉到个位数/分钟）。
            alt = old.get("u:" + hashlib.sha1(u.encode("utf-8")).hexdigest()[:20])
            if alt and os.path.exists(os.path.join(AS, alt)):
                ok[k] = alt
                cached += 1
                continue
            # ★★ 本轮内同一 URL 也只下一次（跨轮次上面已挡住，本轮内还得再挡一次）
            #   todo 里 u:主键 和 skey 别名会指向同一个 URL。若各自提交任务，同一张图会被
            #   下载 + WebP 编码两遍 —— 实测这让待办从 3.7 万虚增到 7 万，白烧一半 CPU 和
            #   一半流量。所以按 URL 去重：主键真下，别名记进 pend_alias，下完统一回填。
            if u in seen_url:
                pend_alias.append((k, u))
                continue
            seen_url[u] = k
            # ★★ 2026-10-08 血案修复（本坑第二处）：本轮新增计数**必须用 len(futs)**。
            #   旧写法 `len(ok) - cached`：调度阶段 `ok` 只从上面「已缓存」两个分支灌入
            #   （只有那两处 cached += 1），所以 `len(ok) == cached` **恒成立**
            #   → `len(ok) - cached == 0` 恒为 0 → `max_new` 与 `hard_cap` 在调度阶段
            #   **完全失效**，只有时间预算（budget_s）能刹车。
            #   后果：上面把 disk_files 修好、门一打开，一轮就会把整批 3.6 万条全排上队
            #   → 一次性 +580MB，顶穿 asset_guard 的 700MB 线、push 被拒。
            #   `futs` 才是「本轮真正排上的下载任务数」，用它计数两个上限才真正生效；
            #   且 `_save` 只在收尾循环里对已完成任务执行 → 未完成的任务不会在磁盘留孤儿。
            if len(futs) >= max_new or disk_files + len(futs) >= hard_cap:
                skipped_budget += 1
                continue
            futs[ex.submit(_get, u)] = (k, u)
        url_rel = {}           # url → 本轮下好的相对路径（供别名回填）
        # ★★ 2026-10-06 血案修复：checkpoint 计数
        #   事故：本函数**只在全部跑完后一次性写索引**。一轮跑到 3 万张时进程被中断
        #   （预算到点/会话结束/被 kill），收尾那段没执行 → 这一轮下好的图全在磁盘上，
        #   但"键→文件"的映射没入库 → 变成谁也认不出的孤儿（实测累积 31574 张 / 600MB）。
        #   端上查封面是按 skey 查索引的，索引里没有 = 图等于没下（"沉淀白做"）。
        #   所以每 1000 张成功落盘就抢写一次索引：中断最多丢 1000 张的键。
        _ckpt_done = 0
        for i, f in enumerate(as_completed(futs), 1):
            if time.time() - t0 > budget_s:
                skipped_budget += len(futs) - i
                break
            k, u = futs[f]
            raw = None
            try:
                raw = f.result()
            except Exception:
                raw = None
            if not raw:
                fail += 1
                continue
            rel, n = _save(kind, raw)
            if rel:
                ok[k] = rel
                url_rel[u] = rel
                total += n
            else:
                fail += 1
            if i % 200 == 0:
                log("   ...%d/%d  用时 %.0fs" % (i, len(futs), time.time() - t0))
            # ★ checkpoint：每 1000 张成功落盘抢写一次索引（原子替换，不破坏已有文件）
            if len(ok) - cached - _ckpt_done >= 1000:
                _ckpt_done = len(ok) - cached
                try:
                    ck = dict(old)
                    ck.update(ok)
                    _atomic_index(op, kind, ck, len(ok) - cached, cached, fail,
                                  skipped_budget, note="checkpoint（中途落盘，防中断丢键）")
                    log("   ...checkpoint 索引已落盘（%d 键）" % len(ck))
                except Exception as e:
                    log("   ...checkpoint 失败（不阻断）：%s" % e)
        # ★ 别名回填：同一 URL 的 skey 别名直接指向主键下好的那张图，不再重复下载
        alias_filled = 0
        for k, u in pend_alias:
            rel = url_rel.get(u) or old.get("u:" + hashlib.sha1(u.encode("utf-8")).hexdigest()[:20])
            if rel and os.path.exists(os.path.join(AS, rel)):
                ok[k] = rel
                alias_filled += 1
        if pend_alias:
            log("   ...别名回填 %d/%d（省下同量重复下载）" % (alias_filled, len(pend_alias)))
    allmap = dict(old)
    allmap.update(ok)
    # ★ 自愈：旧版本用过「歌名|歌手」「mid」当键，新版统一改 URL 去重键（u:xxx）。
    #   两种键并存会让索引虚高（同一张图两个名字）。规则：
    #     · todo 里**这轮明确提到的键**优先保留；
    #     · 不在 todo 里、且对应文件已不存在的键 → 清掉（孤儿键）；
    #     · 旧式非 u:/mv: 前缀的键 → 只在没有任何新式键时才留（向后兼容老端）。
    #   注意：**不能**无脑删旧键 —— 已发布的 App 版本可能还在用旧键查图。
    has_new = any(k.startswith(("u:", "mv:")) for k in allmap)
    if has_new:
        cleaned = {}
        dropped = 0
        for k, rel in allmap.items():
            if k.startswith(("u:", "mv:")):
                cleaned[k] = rel
            elif k in todo:
                cleaned[k] = rel
            else:
                # 旧式键：文件还在就留着（老端兜底），文件没了就扔
                if rel and os.path.exists(os.path.join(AS, rel)):
                    cleaned[k] = rel
                else:
                    dropped += 1
        if dropped:
            log("[%s] 清理孤儿键 %d 个" % (kind, dropped))
        allmap = cleaned
    # ★★ 收尾落盘：走同一个原子写（与 checkpoint 共用，避免两套写法漂移）
    _atomic_index(op, kind, allmap, len(ok) - cached, cached, fail, skipped_budget)
    log("[%s] 完成：%d 张（新增 %d / 复用 %d / 失败 %d / 留待下轮 %d）新增 %s"
        % (kind, len(allmap), len(ok) - cached, cached, fail, skipped_budget,
           ("%.1f MB" % (total / 1048576)) if total > 1048576 else ("%.0f KB" % (total / 1024))))
    return allmap


def load_json(name, default=None):
    try:
        return json.load(open(os.path.join(DATA, name), encoding="utf-8"))
    except Exception:
        return default if default is not None else {}


# ★★ 2026-10-06 关键修复：端上查封面的键，与我们存封面的键**必须逐字符一致**。
#
#   事故：之前 cv 只存了两种键 —— `u:<url的sha20>` 和 `mv:<id>`，
#   而端上歌曲封面是拿 `skey(歌名, 歌手)` 去查的 →
#   **5473 张图存了，端上一条都命中不了，全是死数据**（等于白下载）。
#
#   端上 app.html:1056 & 1079 的定义（这就是唯一契约，别自己另发明一套）：
#     norm = s => String(s||"").toLowerCase()
#                 .replace(/[\s\-_（）()\[\]【】·,.，。'"!！?？~～&]/g, "")
#     skey(t,s) = norm(t).slice(0,40) + "|" + norm(s).slice(0,30)
#
#   Python 侧必须**同一个字符集、同一个顺序、同一个切片长度**：
#     · 先 lowercase（用 casefold 会在某些字符上多转换，这里显式 lower 对齐 JS）
#     · 再按同一类字符做"删除"（不是替换成空格！JS 的 replace 是删掉）
#     · 中文标点常被漏写，这里一个都不能少：·，。、'"！？～ 等
_NORM_DROP = re.compile(u"[\\s\\-_()（）\\[\\]【】·,.，。'\"!！?？~～&]")
# ↑ 与 JS 正则字面量一一对应；JS 里 \[ \] ( ) 在字符类中是字面量，Python 同理


def _js_norm(s):
    """与端上 app.html 的 norm() 逐字符等价的归一化（改了这边必须同步那边）"""
    return _NORM_DROP.sub("", str(s or "").lower())


def _js_skey(t, s):
    """与端上 skey() 等价的键：歌名截 40 + '|' + 歌手截 30（都是归一化**之后**再截）"""
    return _js_norm(t)[:40] + "|" + _js_norm(s)[:30]


def _walk(x, key, acc):
    """递归收所有 key 字段的字符串值（分类/榜单是深层嵌套结构）"""
    if isinstance(x, dict):
        v = x.get(key)
        if isinstance(v, str) and v:
            acc.append(v)
        for k, vv in x.items():
            if k != key:
                _walk(vv, key, acc)
    elif isinstance(x, list):
        for v in x:
            _walk(v, key, acc)


def _walk2(x, k1, k2, acc):
    """递归收 (k1, k2) 成对字段，喂给 acc 作为 (名, 歌手) 元组。

    ★ 为什么需要：曲库/歌库的封面要按「歌名|歌手」建索引，就得同时拿到这两个字段。
      实测曲库分片字段：{n: 歌名, a: 歌手, p: 封面URL, pop: 热度}
      实测歌库字段：    {t: 歌名, s: 歌手, cov: 封面URL}
    """
    if isinstance(x, dict):
        v1, v2 = x.get(k1), x.get(k2)
        if isinstance(v1, str) and v1 and isinstance(v2, str):
            acc.append((v1, v2))
        for k, vv in x.items():
            if k not in (k1, k2):
                _walk2(vv, k1, k2, acc)
    elif isinstance(x, list):
        for v in x:
            _walk2(v, k1, k2, acc)


def _title_index(fields, pairs):
    """(歌名, 歌手) 对 → skey（端上可命中）。

    fields: (歌名字段名, 歌手字段名)；pairs: [(歌名, 歌手, 封面URL), ...]
    返回 {skey: 封面URL}，同键只留第一个（热门前置时即留最热的）。
    """
    k1, k2 = fields
    out = {}
    for t, s, u in pairs:
        if not (t and u and u.startswith("http")):
            continue
        k = _js_skey(t, s)
        if k not in out:
            out[k] = u
    return out


def _lib_covers(top):
    """从曲库分片里按热度取前 top 首的封面，返回 [(pop, 歌名, 歌手, 封面URL), ...]（已降温）。

    ★ 为什么要按热度：曲库 101256 首共 70987 张唯一封面 ≈ 832MB，超 GitHub
      1GB 仓库上限。按热度分层取，先保 App 真会点到的歌；冷门的留待后续轮次
      （或永远走远程 —— 端上是"本地优先、远程兜底"，不是非此即彼）。

    ★ 为什么带上歌名/歌手：调用方要用 (_js_skey(歌名, 歌手)) 建端上可命中的索引，
      只给 URL 的话索引建不出来（这正是 2026-10-06 修的那个"存了 5473 张端上一张用不上"的坑）。
      分片字段实测：{n: 歌名, a: 歌手, p: 封面URL, pop: 热度}
    """
    import glob
    files = sorted(glob.glob(os.path.join(DATA, "catalog", "shard-*.json")))
    rows = []
    for f in files:
        try:
            arr = json.load(open(f, encoding="utf-8"))
        except Exception:
            continue
        for s in arr:
            u = (s.get("p") or "").strip()
            if u.startswith("http"):
                rows.append((s.get("pop") or 0, s.get("n") or "", s.get("a") or "", u))
    rows.sort(key=lambda x: -x[0])
    return rows[:top]


def cmd_localize():
    """主入口：把全站远程图片本地化（头像 / KV / 歌库封面 / MV 封面 / 分类封面 / 曲库封面）"""
    os.makedirs(AS, exist_ok=True)
    t0 = time.time()

    # ① 歌手头像（artists.json: artists[].n → .av）
    #    ★ 按热度（seen）降序 —— 体量封顶时先保热门歌手，冷门的留待后续轮次
    arts = load_json("artists.json", {})
    av_map = {}
    for a in sorted((arts.get("artists") or []), key=lambda x: -(x.get("seen") or 0)):
        n, u = (a.get("n") or "").strip(), (a.get("av") or "").strip()
        if n and u:
            av_map[n] = u
    if av_map:
        run_kind("av", av_map)

    # ② 首页 KV 海报（hero.json: items[].cov）
    hero = load_json("hero.json", {})
    kv_map = {}
    for it in (hero.get("items") or []):
        key = (it.get("albummid") or "") or ((it.get("t") or "") + "|" + (it.get("s") or ""))
        u = (it.get("cov") or "").strip()
        if key and u:
            kv_map[key] = u
    if kv_map:
        run_kind("kv", kv_map)

    # ③ 歌库封面 + ④ MV 全量封面 + ⑤ 分类封面 + ⑥ 榜单曲目封面 + ⑦ 曲库分层封面
    #    全部进 cv 规格（400×400）。★ 键的设计：
    #      · 歌库/榜单/分类：用**封面 URL 本身归一化后的 sha** 当键 —— 同一张图跨栏目只下一次！
    #        （实测 library 715 张 与 categories 2337 张交集为 0，但同专辑多曲共用很多，
    #          用 URL 当键能天然去重，比用歌名/mid 当键省一大截下载量。）
    #      · MV：用 "mv:<id>" 当键（MV 封面 URL 不带稳定标识，用 id 更可靠）
    cv_map = {}

    def cov_key(u):
        """封面 URL → 稳定键（跨栏目天然去重）"""
        return "u:" + hashlib.sha1(u.encode("utf-8")).hexdigest()[:20]

    # ④ MV 全量封面（新版 mv.json 有 5 个栏目共 ~1200 条）
    mv = load_json("mv.json", {})
    n_mv = 0
    for c in (mv.get("collections") or []):
        for m in sorted((c.get("items") or []), key=lambda x: -(x.get("play") or 0)):
            u = (m.get("cov") or "").strip()
            if m.get("id") and u.startswith("http"):
                cv_map["mv:" + str(m["id"])] = u
                n_mv += 1

    # ⑤ 分类封面（categories.json 深层嵌套，3379 处）—— 之前完全没本地化，是全站最大缺口
    cats = load_json("categories.json", {})
    acc = []
    _walk(cats, "cov", acc)
    n_cat = 0
    for u in set(acc):
        if u.startswith("http"):
            cv_map[cov_key(u)] = u
            n_cat += 1

    # ⑥ 榜单曲目封面（charts.json 998 处）
    #    ★ 榜单曲目是 {title, singer, cover} —— 这里同时建**歌名|歌手键**，
    #      端上点榜单里的歌、播放页/歌词页取封面就能命中我们自己的图。
    ch = load_json("charts.json", {})
    acc2 = []
    _walk(ch, "cover", acc2)
    n_ch = 0
    for u in set(acc2):
        if u.startswith("http"):
            cv_map[cov_key(u)] = u
            n_ch += 1
    ch_pairs = []
    for lst in (ch.get("lists") or []):
        for s in (lst.get("songs") or []):
            ch_pairs.append((s.get("title"), s.get("singer"), s.get("cover")))
    # 热搜关键词条（hotkeys）没有封面字段，跳过；榜单曲目已覆盖
    for k, u in _title_index(("title", "singer"), ch_pairs).items():
        cv_map.setdefault(k, u)
    n_ti = len(_title_index(("title", "singer"), ch_pairs))
    log("cv 歌名索引：榜单 %d 条键" % n_ti)

    # ③ 歌库封面（library.json）★ 同时建歌名|歌手键（端上歌库/播放页靠它命中）
    lib = load_json("library.json", {})
    acc3 = []
    _walk(lib, "cov", acc3)
    n_lib = 0
    for u in set(acc3):
        if u.startswith("http"):
            cv_map[cov_key(u)] = u
            n_lib += 1
    lib_pairs = [(s.get("t"), s.get("s"), s.get("cov")) for s in (lib.get("songs") or [])]
    for k, u in _title_index(("t", "s"), lib_pairs).items():
        cv_map.setdefault(k, u)
    n_ti2 = len(_title_index(("t", "s"), lib_pairs))
    log("cv 歌名索引：歌库 %d 条键" % n_ti2)

    # ⑦ 曲库分层封面（catalog 分片，按热度前 CV_LIB_TOP 首）
    #    ★★ 这里同样补歌名|歌手键 —— 曲库是 App 里曲子最多的来源，
    #       没有这条键，播放页封面 100% 白图（远程一挂就全废）。
    #       字段实测：{n: 歌名, a: 歌手, p: 封面URL, pop: 热度}
    n_cat_lib = 0
    n_ti3 = 0
    if CV_LIB_TOP > 0:
        lib_rows = _lib_covers(CV_LIB_TOP)          # [(pop, 歌名, 歌手, url), ...] 已按热度降序
        for _pop, _t, _s, u in lib_rows:
            k = cov_key(u)
            if k not in cv_map:
                cv_map[k] = u
                n_cat_lib += 1
        pairs3 = [(t, s, u) for _p, t, s, u in lib_rows]
        ti3 = _title_index(("n", "a"), pairs3)
        for k, u in ti3.items():
            cv_map.setdefault(k, u)
        n_ti3 = len(ti3)
        log("cv 歌名索引：曲库(热度前 %d) %d 条键" % (CV_LIB_TOP, n_ti3))

    log("cv 待办分解：MV %d / 分类 %d / 榜单 %d / 歌库 %d / 曲库(热度前 %d) %d → 合计唯一 %d"
        % (n_mv, n_cat, n_ch, n_lib, CV_LIB_TOP, n_cat_lib, len(cv_map)))
    log("cv 索引构成：歌名|歌手键 %d（端上可命中）/ URL键 若干 / mv:<id> 若干" % (n_ti + n_ti2 + n_ti3))
    if cv_map:
        run_kind("cv", cv_map)

    # 总账
    man = {"updated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
           "kinds": {}, "elapsed_s": int(time.time() - t0)}
    total_bytes = 0
    for k in SPEC:
        p = os.path.join(AS, k + ".json")
        if os.path.exists(p):
            d = json.load(open(p, encoding="utf-8"))
            d2 = os.path.join(AS, SPEC[k]["dir"])
            b = sum(os.path.getsize(os.path.join(d2, f))
                    for f in os.listdir(d2)) if os.path.isdir(d2) else 0
            man["kinds"][k] = {"count": d.get("count"), "fail": d.get("fail"),
                               "deferred": d.get("deferred"), "bytes": b}
            total_bytes += b
    man["total_bytes"] = total_bytes
    json.dump(man, open(os.path.join(AS, "manifest.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    log("本地化总账：%s  共 %.1f MB  用时 %ds"
        % ({k: v["count"] for k, v in man["kinds"].items()}, total_bytes / 1048576,
           man["elapsed_s"]))


def cmd_stat():
    """只体检，不下载：统计各栏目里还有多少是「别人的 URL」"""
    log("== 图片本地化体检（全站）==")
    checks = [
        ("歌手头像", "artists.json", "av"),
        ("首页KV", "hero.json", "cov"),
        ("歌库封面", "library.json", "cov"),
        ("MV封面", "mv.json", "cov"),
        ("分类封面", "categories.json", "cov"),
        ("榜单封面", "charts.json", "cover"),
    ]
    grand = 0
    for name, fn, uk in checks:
        d = load_json(fn, {})
        if not d:
            log("  %-8s (文件缺失)" % name)
            continue
        acc = []
        _walk(d, uk, acc)
        uniq = set(acc)
        remote = [u for u in uniq if u.startswith("http")]
        grand += len(remote)
        log("  %-8s 唯一 %-6d 远程 URL %-6d" % (name, len(uniq), len(remote)))
    log("  合计待本地化（不含曲库分片）：%d 张" % grand)
    for k in SPEC:
        p = os.path.join(AS, k + ".json")
        if os.path.exists(p):
            d = json.load(open(p, encoding="utf-8"))
            log("  已本地化[%s]: %d 张（本轮新增 %s / 复用 %s / 失败 %s / 待下轮 %s）"
                % (k, d.get("count"), d.get("new"), d.get("cached"),
                   d.get("fail"), d.get("deferred")))

    # ★★ 2026-10-06 新增：**端上可命中率**体检。
    #   光看"存了多少张"没意义 —— 存了 5473 张、端上一条查不到，等于零。
    #   这里按端上真实的三种取键方式各算一遍覆盖率，缺哪条一眼看出。
    log("")
    log("== 端上取图契约覆盖率（这才是「有没有用」的真指标）==")
    cvp = os.path.join(AS, "cv.json")
    if os.path.exists(cvp):
        cm = (json.load(open(cvp, encoding="utf-8")) or {}).get("map") or {}
        # ① skey 键（歌名|歌手）—— 歌曲/歌库/榜单封面靠它
        n_skey = sum(1 for k in cm if not k.startswith(("u:", "mv:")))
        # ② mv:<id> 键 —— MV 卡/详情页靠它
        n_mvk = sum(1 for k in cm if k.startswith("mv:"))
        n_uk = sum(1 for k in cm if k.startswith("u:"))
        log("  cv 索引构成：歌名|歌手键 %d / mv:<id> 键 %d / u:URL 键 %d"
            % (n_skey, n_mvk, n_uk))

        # 逐栏目算"该栏目的条目能命中多少"
        try:
            lib = load_json("library.json", {})
            lk = {_js_skey(s.get("t"), s.get("s")) for s in (lib.get("songs") or [])}
            hit = sum(1 for k in lk if k in cm)
            log("  歌库：%d 首，命中 %d（%.1f%%）" % (len(lk), hit, 100.0*hit/max(1, len(lk))))
        except Exception as e:
            log("  歌库覆盖率计算失败：%s" % e)
        try:
            mv = load_json("mv.json", {})
            ids = [m.get("id") for c in (mv.get("collections") or []) for m in (c.get("items") or [])]
            hit = sum(1 for i in ids if ("mv:" + str(i)) in cm)
            log("  MV：%d 条，命中 %d（%.1f%%）" % (len(ids), hit, 100.0*hit/max(1, len(ids))))
        except Exception as e:
            log("  MV 覆盖率计算失败：%s" % e)
        try:
            ch = load_json("charts.json", {})
            ck = {_js_skey(s.get("title"), s.get("singer"))
                  for l in (ch.get("lists") or []) for s in (l.get("songs") or [])}
            hit = sum(1 for k in ck if k in cm)
            log("  榜单：%d 首，命中 %d（%.1f%%）" % (len(ck), hit, 100.0*hit/max(1, len(ck))))
        except Exception as e:
            log("  榜单覆盖率计算失败：%s" % e)
        try:
            cat = _lib_covers(CV_LIB_TOP)
            kk = {_js_skey(t, s) for _p, t, s, _u in cat}
            hit = sum(1 for k in kk if k in cm)
            log("  曲库(热度前 %d)：%d 首，命中 %d（%.1f%%）"
                % (CV_LIB_TOP, len(kk), hit, 100.0*hit/max(1, len(kk))))
        except Exception as e:
            log("  曲库覆盖率计算失败：%s" % e)
    else:
        log("  cv.json 不存在 —— 端上一张本地图都用不上")


def cmd_mkjs():
    """从已有的 assets/<kind>.json 重新导出 assets/<kind>.js（不下载、不联网）。

    用途：索引早就生成好了、只是缺 .js 那一份（端上 file:// 拿不到 fetch，
    只能靠 <script>）。秒级完成，不必重跑 localize。
    """
    n = 0
    for k in SPEC:
        jp = os.path.join(AS, k + ".json")
        if not os.path.exists(jp):
            log("[%s] 无索引，跳过" % k)
            continue
        try:
            d = json.load(open(jp, encoding="utf-8"))
        except Exception as e:
            log("[%s] 读取失败：%s" % (k, e))
            continue
        p = _write_js(k, d)
        log("[%s] → %s（%d 条键，%.1f KB）"
            % (k, os.path.basename(p), len(d.get("map") or {}), os.path.getsize(p) / 1024))
        n += 1
    log("完成：%d 份 .js（端上 file:// 只能靠 <script> 拿索引，fetch 会被 WebView 拦）" % n)


def cmd_prune():
    """孤儿清理：删掉**索引里没人引用**的图片。

    为什么必须有（不是洁癖，是仓库活命问题）：
      我们的图片是**内容寻址**命名（文件名 = sha1(webp 内容)[:16]）。
      好处：重跑幂等，同名直接覆盖，不会产生重复。
      代价：**上游一旦换图**（同一位歌手上传了新头像、同一首歌换了封面），
            新图落地会拿到一个新文件名，而**旧文件没有任何人删它** ——
            它会永远躺在 data/assets/ 里，且被 git 永久记进历史。
      曲库 10 万首、头像 4000 位、封面几万张，跑上几个月这就是几百 MB 的纯垃圾，
      而 asset_guard 量的只是**工作区**体量，量不出 git 历史 —— 仓库会先炸历史。

    安全边界（宁可不治也不能错治）：
      · 默认 **dry-run**，只打印不删；确认真实要删的量之后再 `--yes`。
      · 索引缺失 / 索引为空（map 条数 0）→ **直接拒绝执行**，绝不"全删"。
      · 只删 SPEC 里那三个 kind 目录下的 .webp，绝不动别的文件。
      · 删除前先落 `assets/prune.log`，留痕可追溯。
    """
    yes = "--yes" in sys.argv
    log("== 孤儿清理（%s）==" % ("真删" if yes else "试跑 dry-run，不删任何东西"))
    total_orphan = 0
    total_kept = 0
    lines = []
    for k in SPEC:
        jp = os.path.join(AS, k + ".json")
        if not os.path.exists(jp):
            log("[%s] 索引不存在 —— 拒绝清理该目录" % k)
            continue
        try:
            d = json.load(open(jp, encoding="utf-8"))
        except Exception as e:
            log("[%s] 索引读取失败（%s）—— 拒绝清理该目录" % (k, e))
            continue
        mp = d.get("map") or {}
        if not mp:
            log("[%s] 索引为空 —— 拒绝清理该目录（防误删全站图）" % k)
            continue
        alive = set(os.path.basename(v) for v in mp.values() if v)
        dd = os.path.join(AS, k)
        if not os.path.isdir(dd):
            log("[%s] 目录不存在，跳过" % k)
            continue
        orphans = []
        for f in sorted(os.listdir(dd)):
            if not f.lower().endswith(".webp"):
                continue
            if f in alive:
                total_kept += 1
            else:
                orphans.append(f)
        total_orphan += len(orphans)
        log("[%s] 索引引用 %d 张 / 磁盘 %d 张 → 孤儿 %d 张"
            % (k, len(alive), len(alive) + len(orphans), len(orphans)))
        for f in orphans:
            lines.append("%s\t%s" % (k, f))
        if yes and orphans:
            n = 0
            for f in orphans:
                try:
                    os.remove(os.path.join(dd, f))
                    n += 1
                except OSError as e:
                    log("[%s] 删除失败 %s：%s" % (k, f, e))
            log("[%s] 已删除 %d 张" % (k, n))
    if lines:
        try:
            with open(os.path.join(AS, "prune.log"), "a", encoding="utf-8") as fh:
                fh.write("# %s  %s\n" % (time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                                         "已删" if yes else "dry-run"))
                fh.write("\n".join(lines) + "\n")
        except Exception:
            pass
    log("合计：有效 %d 张 / 孤儿 %d 张%s"
        % (total_kept, total_orphan, "" if yes else "（dry-run，未删；确认无误后加 --yes 真删）"))


if __name__ == "__main__":
    cmd = (sys.argv[1] if len(sys.argv) > 1 else "stat").lower()
    if cmd in ("localize", "all", "run"):
        cmd_localize()
    elif cmd in ("mkjs", "syncjs"):
        cmd_mkjs()
    elif cmd in ("prune", "gc", "orphan"):
        cmd_prune()
    else:
        cmd_stat()
