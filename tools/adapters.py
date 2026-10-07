#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
adapters.py —— 我们自己的音源适配器（零第三方依赖 / 零第三方解析服务）

每个适配器实现两个动作：
  search(title, singer) -> list[item]      归一化后的候选项
  resolve(item)         -> {url, quality, ext} | None

所有签名算法（QQ zzb、酷狗 salt-md5）都在本文件内自己实现，不调用任何外部解析 API。
平台只要换域名/字段，改动只发生在这里；上层（pool / App）完全无感 —— 这就是「把别人的变成自己的」。
"""
import base64, hashlib, json, random, re, time, urllib.parse, urllib.request, urllib.error

UA_PC = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
         "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36")
UA_ANDROID = "Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Mobile Safari/537.36"
TIMEOUT = 18


def _md5(s):
    return hashlib.md5(s.encode("utf-8", "ignore") if isinstance(s, str) else s).hexdigest()


def _randhex(n=32):
    return "".join(random.choice("0123456789abcdef") for _ in range(n))


def http(url, headers=None, data=None, read=0, follow=True, timeout=TIMEOUT):
    h = {"User-Agent": UA_PC, "Accept": "*/*", "Accept-Language": "zh-CN,zh;q=0.9"}
    if headers:
        h.update(headers)
    op = urllib.request.build_opener()
    if not follow:
        class _NR(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *a, **k):
                return None
        op = urllib.request.build_opener(_NR)
    t0 = time.time()
    try:
        with op.open(urllib.request.Request(url, data=data, headers=h), timeout=timeout) as r:
            return {"status": r.status, "body": r.read(read) if read else r.read(),
                    "ctype": r.headers.get("Content-Type", ""), "loc": r.headers.get("Location", ""),
                    "sc": r.headers.get_all("Set-Cookie") or [], "ms": int((time.time() - t0) * 1000)}
    except urllib.error.HTTPError as e:
        return {"status": e.code, "body": (e.read(6000) if e.fp else b""), "ctype": "",
                "loc": (e.headers.get("Location", "") if e.headers else ""),
                "sc": (e.headers.get_all("Set-Cookie") or []) if e.headers else [],
                "ms": int((time.time() - t0) * 1000)}
    except Exception as e:
        return {"status": None, "body": b"", "ctype": "", "loc": "", "sc": [],
                "ms": int((time.time() - t0) * 1000), "err": f"{type(e).__name__}: {e}"}


def jload(r):
    try:
        return json.loads(r["body"].decode("utf-8", "replace"))
    except Exception:
        return None


AUDIO_MAGIC = {
    "ID3": "mp3", b"\xff\xfb": "mp3", b"\xff\xf3": "mp3", b"\xff\xf2": "mp3",
    "fLaC": "flac", "OggS": "ogg", "RIFF": "wav",
}


def sniff(b):
    """文件头嗅探：返回 (是否音频, 扩展名)"""
    if not b or len(b) < 16:
        return False, "?"
    if b[:3] == b"ID3" or b[:2] in (b"\xff\xfb", b"\xff\xf3", b"\xff\xf2"):
        return True, "mp3"
    if b[4:8] == b"ftyp":
        return True, "m4a"
    if b[:4] == b"fLaC":
        return True, "flac"
    if b[:4] == b"OggS":
        return True, "ogg"
    if b[:4] == b"RIFF":
        return True, "wav"
    if b[:1] == b"<":
        return False, "html"
    return False, b[:4].hex()


def verify_playable(url, referer=None, nbytes=4096):
    """唯一判据：Range GET 拿回真音频字节"""
    if not url or not url.startswith("http"):
        return False, "无URL", None
    h = {"Range": f"bytes=0-{nbytes-1}"}
    if referer:
        h["Referer"] = referer
    r = http(url, headers=h, read=nbytes)
    ok, ext = sniff(r["body"])
    good = r["status"] in (200, 206) and ok
    return good, f"HTTP {r['status']} {len(r['body'])}B {ext}", ext


def norm(s):
    return re.sub(r"[\s\-_（）()\[\]【】·,.，。'\"!！?？~～&]", "", (s or "")).lower()


def dur_s(v):
    """把各源千奇百怪的时长统一成**秒**（`_score_cand` 的时长硬毙全靠它）。

      · QQ / 酷我 / 酷狗 回的是**秒**（interval / DURATION / duration）
      · 咪咕回的是 **"mm:ss" 字符串**，部分字段回**毫秒**
      · 网易云回**毫秒**
    判据：字符串带 ":" → 按时间来解；数值 > 3600 → 当毫秒（1 小时以上的歌极少，
    真到那个量级的数值只可能是毫秒）。
    """
    if v is None:
        return 0
    if isinstance(v, str):
        v = v.strip()
        if not v:
            return 0
        if ":" in v:
            try:
                p = [int(z) for z in v.split(":")]
                while len(p) < 3:
                    p.insert(0, 0)
                return p[0] * 3600 + p[1] * 60 + p[2]
            except Exception:
                return 0
        try:
            v = float(v)
        except Exception:
            return 0
    try:
        n = float(v)
    except Exception:
        return 0
    if n <= 0:
        return 0
    return int(n / 1000) if n > 3600 else int(n)


# ── 劣质变体关键词（★ 2026-10-07 拆成「硬毙 / 软罚」两档）──────────────────────
# 为什么拆（实测血案，驱动自主人第五轮质问「为什么就死盯着网易呢」）：
#   QQ 热歌榜前 24 首试跑多源回退，酷我这一路挑出来的全是脏的：
#     搜「江南 林俊杰」   → 《江南 (DJ 阿树版)》
#     搜「我们俩 郭顶」   → 《我们俩 (原版伴奏)》
#     搜「开始懂了 孙燕姿」→ 《开始懂了 (片段版)-2017"反正都精彩"浙江卫视》
#     搜「红色高跟鞋 蔡健雅」→《红色高跟鞋 (DJ 咚鼓版)》
#     搜「唯一 G.E.M.」   → 《唯一 (TRAP Remix)》
#     B站搜「恋人 李荣浩」→ 《李荣浩⑧专5单《恋人》MV 现已正式上线》
#   根因不是打分公式，而是**候选池里压根没有干净版本**，旧逻辑（4 分/词）照样
#   把脏的选出来当"能播的替代"。这直接违反主人两条铁律：
#     「翻唱尽量不要吧因为质量太差」+「宁可不治也不能错治」。
#
# HARD_BAD：候选指向的根本不是「这首歌的某个版本」，而是另一种东西 → **直接毙**。
#   ⚠️ 必须硬毙而不是处罚：一旦放行，这歌在端上就变成"能播但播出来是伴奏/宣传片"，
#      比"暂时播不了"糟糕得多（错治）。宁可这首歌这轮没有直链，等下一轮再说。
HARD_BAD = ("伴奏", "纯音乐", "instrumental", "karaoke", "卡拉ok", "off vocal", "offvocal",
            "片段", "试听", "铃声", "剪辑", "教学", "教程", "简谱", "琴谱", "曲谱",
            "鼓谱", "乐谱", "动态鼓谱", "谱", "示范", "翻弹",
            "钢琴曲", "吉他曲", "八音盒", "口琴", "萨克斯", "二胡", "古筝",
            # ★ 二次加工（2026-10-07 第二轮收紧）：这些**不是原唱**，且实测酷我搜索
            #   一池子全是这些（江南/演员/多远都要在一起/唯一 全被 DJ 版和 Remix 占满）。
            #   主人：「翻唱尽量不要吧因为质量太差」——DJ/Remix 比翻唱还差，必须硬毙。
            "dj", "remix", "remix版", "混音", "慢摇", "车载", "8d", "环绕", "广场舞",
            "抖音", "串烧", "变速", "变调",
            # 视频源的"歌"其实不是歌：MV 上线通稿 / 预告 / 花絮 / 采访
            "上线", "首发", "预告", "花絮", "采访", "宣传片",
            # 非音乐内容（英语听力 / 考试真题 / 有声书 / 课文朗读）
            #   来由：2026-10-06 深夜 harvest 被自己的发布前体检拦下，抽样坏例子是
            #     「奥巴马会见格鲁吉亚总统(1/—英语听力；第三期 2006年6月真题 —英语听力」
            "听力", "真题", "英语", "四级", "六级", "雅思", "托福", "单词", "音标",
            "课文", "朗读", "背诵", "有声书", "有声小说", "评书", "相声", "讲座",
            "教材", "试卷", "考题", "口语", "语法", "课件")
# SOFT_BAD：**还是原唱本人在唱**，只是非录音室原版 —— 能听，让位给干净版即可。
#   ⚠️ 翻唱/cover 放这一档（不放硬毙）：主人明确「如果是好的翻唱也可以接受」。
SOFT_BAD = ("翻唱", "cover", "合唱", "现场", "live", "演唱会", "巡演", "tour",
            "encore", "演奏会", "音乐会", "私藏", "歌单", "合集", "mv", "无损音乐馆",
            "治愈", "睡前", "助眠", "慢速", "加速", "字幕", "动态歌词", "完整版",
            "和声", "改版", "新版", "铃声版")

# 试听片段/副歌剪辑的判定下限：正常歌极少低于 90 秒
MIN_CAND_DUR = 90
# 与目标时长（QQ 榜单/原曲元数据）的容差：差超过 35% 或 45 秒 → 认定不是同一版
DUR_TOL_ABS = 45
DUR_TOL_REL = 0.35

# 保留旧名（pick / pick_all 仍在用，语义 = 「命中即降权」）
BAD_WORDS = tuple(dict.fromkeys(HARD_BAD + SOFT_BAD))


def badness(text):
    t = (text or "").lower()
    return sum(1 for w in BAD_WORDS if w in t)


def hard_bad(text):
    """是否指向「根本不是这首歌」的内容（伴奏/片段/谱/教学/宣传片/非音乐）"""
    t = (text or "").lower()
    return any(w in t for w in HARD_BAD)


def soft_bad(text):
    """劣质变体命中数（DJ/Remix/演唱会/翻唱）—— 能用，但必须让位给干净版本"""
    t = (text or "").lower()
    return sum(1 for w in SOFT_BAD if w in t)


def pick(items, title, singer, tf, sf):
    """标题必须吻合；歌手吻合加权；劣质变体降权；同分取标题更短（更贴近原曲）"""
    t, s = norm(title), norm(singer)
    best, best_key = None, None
    for it in items:
        ti, si = norm(tf(it)), norm(sf(it))
        score = 0
        if t and t == ti:
            score = 10
        elif t and (t in ti or ti in t):
            score = 6
        if score == 0:
            continue
        if s and (s in si or si in s):
            score += 5
        score -= 3 * badness(tf(it))                 # 劣质变体扣分
        if t and t == ti:
            score += 2                               # 精确吻合再加权
        key = (score, -len(norm(tf(it))))
        if best_key is None or key > best_key:
            best, best_key = it, key
    return best if best_key and best_key[0] >= 6 else None


def pick_all(items, title, singer, tf, sf, limit=4):
    """返回按质量排序的候选列表（第一候选失败可顺延），供同源多候选回退用"""
    t, s = norm(title), norm(singer)
    ranked = []
    for it in items:
        ti, si = norm(tf(it)), norm(sf(it))
        score = 0
        if t and t == ti:
            score = 10
        elif t and (t in ti or ti in t):
            score = 6
        if score == 0:
            continue
        if s and (s in si or si in s):
            score += 5
        score -= 3 * badness(tf(it))
        if t and t == ti:
            score += 2
        if score < 6:
            continue
        ranked.append((score, -len(norm(tf(it))), it))
    ranked.sort(key=lambda x: (x[0], x[1]), reverse=True)
    return [it for _, _, it in ranked[:limit]]


# =============================================================== 咪咕（免费库大、给 SQ 无损）
class Migu:
    id = "migu"; name = "咪咕音乐"; kind = "platform"; homepage = "https://music.migu.cn/"
    HDR = {"Referer": "https://m.music.migu.cn/", "User-Agent": "Android_migu", "channel": "014X013"}
    QUALITY_RANK = {"SQ": 1.0, "HQ": 0.85, "PQ": 0.6, "LQ": 0.45}

    def search(self, title, singer):
        u = ("https://pd.musicapp.migu.cn/MIGUM2.0/v1.0/content/search_all.do?ua=Android_migu&version=5.0.1"
             "&text=" + urllib.parse.quote(f"{title} {singer}") + "&pageNo=1&pageSize=20&searchSwitch=" +
             urllib.parse.quote('{"song":1}'))
        res = (jload(http(u, self.HDR)) or {}).get("songResultData", {}).get("result") or []
        out = []
        for x in res:
            fmts = list(x.get("newRateFormats") or x.get("rateFormats") or [])
            # 只有"无 vip 标记"的格式才可能匿名取链（vip 格式会回 200002）
            free = [f.get("formatType") for f in fmts
                    if not f.get("showTag") or "vip" not in (f.get("showTag") or [])]
            img = ((x.get("imgItems") or [{}])[0] or {}).get("img", "")
            # ★ 2026-10-07：咪咕原来不给时长 → `_score_cand` 的时长硬毙在这一路完全失效，
            #   实测漏出「爱错 (Live)」「开始懂了 (2000台北万人演唱会)」两个现场版。
            dsec = dur_s(x.get("duration") or x.get("length"))
            if not dsec and fmts:
                dsec = dur_s((fmts[0] or {}).get("duration"))
            out.append({"src": self.id, "title": x.get("name", ""),
                        "singer": "/".join(s.get("name", "") for s in x.get("singers", [])),
                        "album": ((x.get("albums") or [{}])[0] or {}).get("name", ""),
                        "cover": img.replace("http://", "https://"),
                        "dur": dsec,
                        "free": bool(free),
                        "qualitys": [q for q in ("SQ", "HQ", "PQ") if q in free] or ["PQ"],
                        "raw": {"copyrightId": x.get("copyrightId"), "contentId": x.get("contentId"),
                                "vip": not bool(free)}})
        return out

    def resolve(self, item):
        raw = item["raw"]
        if raw.get("vip"):
            self.last_reason = "VIP 受限（咪咕匿名仅免费曲；回 200002）"
            return None
        for flag in [q for q in ("SQ", "HQ", "PQ") if q in item.get("qualitys", [])] or ["PQ"]:
            u = ("https://app.c.nf.migu.cn/MIGUM2.0/v1.0/content/sub/listenSong.do?toneFlag=" + flag +
                 "&netType=00&userId=1554861458870011&ua=Android_migu&version=5.0.1" +
                 f"&copyrightId={raw.get('copyrightId')}&contentId={raw.get('contentId')}"
                 "&resourceType=2&channel=0")
            r = http(u, self.HDR, follow=False)
            tgt = r["loc"] or ""
            if not tgt:
                b = r["body"].decode("utf-8", "replace")[:400]
                if b.startswith("http"):
                    tgt = b.split("\n")[0].strip()
            if tgt.startswith("http"):
                ok, note, ext = verify_playable(tgt, "https://m.music.migu.cn/")
                if ok:
                    return {"url": tgt, "quality": flag, "ext": ext, "note": note}
        self.last_reason = "VIP 受限（回 200002）"
        return None


# =============================================================== B站（曲库极大、320k m4a）
class Bili:
    id = "bili"; name = "哔哩哔哩"; kind = "platform"; homepage = "https://www.bilibili.com/"
    VIDEO = True          # 视频源：作者≠歌手，回退候选不做"同歌手"限制
    QUALITY_RANK = {"320kbps": 1.0, "192kbps": 0.85, "132kbps": 0.7, "64kbps": 0.5}

    def _hdr(self):
        return {"Referer": "https://www.bilibili.com/", "Origin": "https://www.bilibili.com",
                "Cookie": f"buvid3={''.join(random.choice('0123456789ABCDEF') for _ in range(32))}-infoc; b_nut=1700000000"}

    def search(self, title, singer):
        u = ("https://api.bilibili.com/x/web-interface/search/type?search_type=video&keyword=" +
             urllib.parse.quote(f"{title} {singer}"))
        r = http(u, self._hdr())
        vids = ((jload(r) or {}).get("data") or {}).get("result") or []
        out = []
        for v in vids[:20]:
            pic = v.get("pic") or ""
            out.append({"src": self.id, "title": re.sub(r"<[^>]+>", "", v.get("title") or ""),
                        "singer": v.get("author") or "", "album": "",
                        "cover": ("https:" + pic) if pic.startswith("//") else pic,
                        "qualitys": ["320kbps", "192kbps", "132kbps", "64kbps"],
                        "raw": {"bvid": v.get("bvid"), "play": v.get("play"), "dur": v.get("duration")}})
        return out

    def resolve(self, item):
        hdr = self._hdr()
        bvid = item["raw"].get("bvid")
        v = jload(http(f"https://api.bilibili.com/x/web-interface/view?bvid={bvid}", hdr)) or {}
        cid = (v.get("data") or {}).get("cid")
        if not cid:
            self.last_reason = "无 cid（稿件不可取）"
            return None
        p = jload(http(f"https://api.bilibili.com/x/player/playurl?bvid={bvid}&cid={cid}"
                       "&fnval=4048&fourk=1&qn=127&try_look=1", hdr)) or {}
        au = ((p.get("data") or {}).get("dash") or {}).get("audio") or []
        if not au:
            self.last_reason = "无音频流（仅视频/需登录）"
            return None
        for a in sorted(au, key=lambda x: x.get("bandwidth", 0), reverse=True):
            u = a.get("baseUrl") or a.get("base_url")
            ok, note, ext = verify_playable(u, "https://www.bilibili.com/")
            if ok:
                return {"url": u, "quality": f"{int(a.get('bandwidth', 0)/1000)}kbps", "ext": ext or "m4a", "note": note}
        self.last_reason = "音频流均不可播"
        return None

    # ---------------------------------------------------------- MV / 演唱会（视频分区）
    def search_mv(self, keyword, limit=20):
        """音乐分区(tid=3)搜 MV/现场；返回视频候选（含 bvid/封面/时长/播放量）"""
        u = ("https://api.bilibili.com/x/web-interface/search/type?search_type=video&tids=3&keyword="
             + urllib.parse.quote(keyword))
        vids = ((jload(http(u, self._hdr())) or {}).get("data") or {}).get("result") or []
        out = []
        for v in vids[:limit]:
            pic = v.get("pic") or ""
            out.append({"src": self.id, "kind": "mv", "title": re.sub(r"<[^>]+>", "", v.get("title") or ""),
                        "author": v.get("author") or "", "album": "",
                        "cover": ("https:" + pic) if pic.startswith("//") else pic,
                        "dur": v.get("duration"), "play": v.get("play"),
                        "raw": {"bvid": v.get("bvid")}})
        return out

    # MV 清晰度优先级（数字越大越高）
    MV_QN = {120: "4K", 116: "1080P60", 112: "1080P+", 80: "1080P", 74: "720P60",
             64: "720P", 32: "480P", 16: "360P", 6: "240P"}
    # 取高清晰度的关键：try_look=1（否则匿名只回 480P）
    MV_FNVAL = "fnval=4048&fourk=1&qn=127&try_look=1"

    def resolve_mv(self, item, min_h=720):
        """取 MV 视频+音频双流（匿名实测可到 1080P）。返回 video/audio 两个地址。"""
        hdr = self._hdr()
        bvid = item["raw"].get("bvid")
        v = jload(http(f"https://api.bilibili.com/x/web-interface/view?bvid={bvid}", hdr)) or {}
        d = v.get("data") or {}
        cid = d.get("cid")
        if not cid:
            self.last_reason = "无 cid"
            return None
        p = jload(http(f"https://api.bilibili.com/x/player/playurl?bvid={bvid}&cid={cid}&{self.MV_FNVAL}", hdr)) or {}
        dash = (p.get("data") or {}).get("dash") or {}
        vs = sorted(dash.get("video") or [], key=lambda x: (x.get("id", 0), x.get("bandwidth", 0)), reverse=True)
        au = sorted(dash.get("audio") or [], key=lambda x: x.get("bandwidth", 0), reverse=True)
        if not vs or not au:
            self.last_reason = "无 DASH 流"
            return None
        for vv in vs:
            if vv.get("id", 0) < (80 if min_h >= 1080 else 64):
                continue
            vu = vv.get("baseUrl") or vv.get("base_url")
            r = http(vu, {"Range": "bytes=0-4095", "Referer": "https://www.bilibili.com/"}, read=4096)
            if r["status"] in (200, 206) and (r["body"][4:8] == b"ftyp" or len(r["body"]) > 1024):
                a = au[0]
                auu = a.get("baseUrl") or a.get("base_url")
                return {"video": vu, "audio": auu,
                        "quality": self.MV_QN.get(vv.get("id"), str(vv.get("id"))),
                        "width": vv.get("width"), "height": vv.get("height"),
                        "dur": d.get("duration"), "cover": d.get("pic", ""),
                        "note": f"HTTP {r['status']} {self.MV_QN.get(vv.get('id'),'?')}"}
        self.last_reason = "无达标清晰度流"
        return None


# =============================================================== 网易云（匿名外链口）
class Netease:
    id = "wy"; name = "网易云音乐"; kind = "platform"; homepage = "https://music.163.com/"
    QUALITY_RANK = {"mp3": 0.75, "lossless": 1.0}

    def search(self, title, singer):
        u = ("https://music.163.com/api/search/get?s=" + urllib.parse.quote(f"{title} {singer}") +
             "&type=1&limit=20&offset=0")
        r = http(u, {"Referer": "https://music.163.com/"})
        songs = ((jload(r) or {}).get("result") or {}).get("songs") or []
        return [{"src": self.id, "title": s.get("name", ""),
                 "singer": "/".join(a["name"] for a in s.get("artists", [])),
                 "album": (s.get("album") or {}).get("name", ""),
                 "cover": ((s.get("album") or {}).get("picUrl") or "").replace("http://", "https://"),
                 "qualitys": ["mp3"], "raw": {"id": s.get("id"), "dur": int((s.get("duration") or 0) / 1000)}}
                for s in songs]

    def resolve(self, item):
        sid = item["raw"].get("id")
        r = http(f"https://music.163.com/song/media/outer/url?id={sid}.mp3",
                 {"Referer": "https://music.163.com/"})
        u = r["loc"]
        if not u:
            # 有些情况下 302 被跟随，直接校验落点
            if r["status"] == 200 and r["ctype"].startswith("audio"):
                ok, note, ext = verify_playable(
                    f"https://music.163.com/song/media/outer/url?id={sid}.mp3", "https://music.163.com/")
                return {"url": f"https://music.163.com/song/media/outer/url?id={sid}.mp3",
                        "quality": "mp3", "ext": ext or "mp3", "note": note} if ok else None
            self.last_reason = "VIP/版权受限（outer 无跳转）"
            return None
        ok, note, ext = verify_playable(u, "https://music.163.com/")
        if not ok:
            self.last_reason = "VIP/版权受限（落点非音频）"
        return {"url": u, "quality": "mp3", "ext": ext or "mp3", "note": note} if ok else None


# =============================================================== QQ（元数据/榜单稳；直链需登录）
class QQ:
    id = "qq"; name = "QQ音乐"; kind = "platform"; homepage = "https://y.qq.com/"
    QUALITY_RANK = {"flac": 1.0, "320k": 0.9, "128k": 0.6, "m4a": 0.65}
    HEAD = [21, 4, 9, 26, 16, 20, 27, 30]
    MID = [212, 45, 80, 68, 195, 163, 163, 203, 157, 220, 254, 91, 204, 79, 104, 6]
    TAIL = [18, 11, 3, 2, 1, 7, 6, 25]

    @classmethod
    def sign(cls, payload):
        """zzb 签名：md5(紧凑JSON) -> 字符重排 + 逐字节异或 + base64 变种"""
        x = _md5(json.dumps(payload, separators=(",", ":"), ensure_ascii=False))
        m = [int(x[2 * i:2 * i + 2], 16) for i in range(16)]
        m = [a ^ b for a, b in zip(m, cls.MID)]
        b64 = re.sub(r"[+/=]", "", base64.b64encode(bytes(m)).decode())
        return ("zzb" + "".join(x[i] for i in cls.HEAD) + b64 + "".join(x[i] for i in cls.TAIL)).lower()

    def search(self, title, singer):
        u = ("https://c.y.qq.com/soso/fcgi-bin/client_search_cp?w=" +
             urllib.parse.quote(f"{title} {singer}") + "&format=json&n=20&p=1&new_json=1")
        r = http(u, {"Referer": "https://y.qq.com/"})
        lst = ((jload(r) or {}).get("data") or {}).get("song", {}).get("list") or []
        out = []
        for s in lst:
            alb = s.get("album") or {}
            mid = alb.get("mid") or ""
            out.append({"src": self.id, "title": s.get("title", ""),
                        "singer": "/".join(g.get("name", "") for g in s.get("singer", [])),
                        "album": alb.get("name", ""),
                        "cover": f"https://y.gtimg.cn/music/photo_new/T002R500x500M000{mid}.jpg" if mid else "",
                        "qualitys": ["128k"], "raw": {"mid": s.get("mid"),
                                                      "media_mid": (s.get("file") or {}).get("media_mid"),
                                                      "dur": s.get("interval")}}
            )
        return out

    def resolve(self, item):
        """返回 None 并带原因：QQ 现在对 **未登录** 的一切取值都回 result=104003"""
        raw = item["raw"]
        guid = str(random.randint(10 ** 9, 10 ** 10 - 1))
        com = {"cv": 4747474, "ct": 24, "format": "json", "notice": 0, "platform": "yqq.json",
               "needNewCode": 1, "uin": 0, "g_tk_new_20200303": 5381}
        fn = f"M500{raw['mid']}{raw.get('media_mid') or raw['mid']}.mp3"
        pl = {"comm": com, "req_0": {"module": "vkey.GetVkeyServer", "method": "CgiGetVkey",
                                     "param": {"guid": guid, "songmid": [raw["mid"]], "songtype": [0],
                                               "uin": "0", "loginflag": 1, "platform": "20", "filename": [fn]}}}
        pl["sign"] = self.sign(pl)
        r = http("https://u.y.qq.com/cgi-bin/musicu.fcg",
                 {"Referer": "https://y.qq.com/", "Content-Type": "application/json"},
                 data=json.dumps(pl, separators=(",", ":")).encode())
        d = ((jload(r) or {}).get("req_0") or {}).get("data") or {}
        mi = (d.get("midurlinfo") or [{}])[0]
        if mi.get("result") != 0 or not mi.get("vkey"):
            self.last_reason = f"result={mi.get('result')}（未登录/需账号）"
            return None
        u = (d.get("sip") or [""])[0] + fn + f"?vkey={mi['vkey']}&guid={guid}&uin=0&fromtag=0"
        ok, note, ext = verify_playable(u, "https://y.qq.com/")
        return {"url": u, "quality": "128k", "ext": ext or "mp3", "note": note} if ok else None


# =============================================================== 酷我（★ 2026-10-06 实测可用）
class Kuwo:
    """酷我音乐适配器 —— 实测**唯一免登录就能拿到真实音频直链**的国内大平台。

    为什么加它（血案实证 2026-10-06）：
      主人点名「查走在冷风中原唱必须排第一」+「VIP也想办法能到」。
      网易云免签 outer/url 对 VIP/付费歌返回 `4515 bytes + text/html`（HTML 下载页）→
      我们的音频闸门判它 dead → **伍佰《泪桥》等原唱被整片误杀，只剩免费翻唱**。
      酷我这两条接口匿名可用、无需 token：
        · 搜索  http://search.kuwo.cn/r.s?all=<kw>            （实测 180~3755 条命中）
        · 直链  http://antiserver.kuwo.cn/anti.s?type=convert_url&rid=MUSIC_<id>&format=mp3&response=url
      实测 6 首（含周杰伦/蔡依林/林俊杰等网易云 VIP 大户）→ 5 首 `200 audio/mpeg`，
      伍佰《浪人情歌》4.35MB、蔡依林《倒带》cover 4.10MB 全是完整曲。
    ★ 但酷我**搜索排序很脏**（第一条常是 DJ版/cover），所以本适配器只负责"给候选"，
      选曲交给 `multi_match` 用 badness + 歌手吻合 + 时长 三重判据挑，绝不无脑取第一条。
    """
    id = "kw"; name = "酷我音乐"; kind = "platform"; homepage = "https://www.kuwo.cn/"
    QUALITY_RANK = {"flac": 1.0, "320k": 0.9, "128k": 0.6}

    def __init__(self):
        self.last_reason = ""

    def search(self, title, singer):
        kw = urllib.parse.quote(("%s %s" % (title or "", singer or "")).strip())
        u = ("http://search.kuwo.cn/r.s?all=" + kw +
             "&ft=music&itemset=web_2013&client=kt&pn=0&rn=20&rformat=json&encoding=utf8")
        r = http(u, {"Referer": "https://www.kuwo.cn/"})
        txt = r["body"].decode("utf-8", "replace").replace("'", '"')
        try:
            d = json.loads(txt)
        except Exception:
            return []
        out = []
        for x in (d.get("abslist") or []):
            rid = x.get("DC_TARGETID") or x.get("MUSICRID") or ""
            rid = str(rid).replace("MUSIC_", "").strip()
            if not rid:
                continue
            # ★ 2026-10-07：酷我搜索本身不吐完整封面 url，只给 web_albumpic_short
            #   相对路径（如 120/54/1205492693.jpg）→ 必须自己拼，否则多源曲目
            #   会因「无 cover」被 load_candidates 判成字段不全整批丢掉。
            pic = (x.get("web_albumpic_short") or x.get("web_artistpic_short") or "").strip()
            out.append({"src": self.id,
                        "title": re.sub(r"&nbsp;?", " ", x.get("SONGNAME") or "").strip(),
                        "singer": re.sub(r"&nbsp;?", " ", x.get("ARTIST") or "").strip(),
                        "album": re.sub(r"&nbsp;?", " ", x.get("ALBUM") or "").strip(),
                        "cover": ("https://img1.kuwo.cn/star/albumcover/" + pic) if pic else "",
                        "qualitys": ["128k"],
                        "raw": {"rid": rid, "dur": x.get("DURATION") or 0,
                                "fmt": x.get("FORMATS") or "",
                                # ★ 酷我白送的原唱标记（对应网易云 originCoverType）
                                #   实测值域待确认，先原样带出，由 _score_cand 决定怎么用
                                "oct": x.get("originalsongtype"),
                                "mvflag": x.get("MVFLAG")}})
        return out

    def resolve(self, item):
        rid = (item.get("raw") or {}).get("rid")
        if not rid:
            self.last_reason = "无 rid"
            return None
        u = ("http://antiserver.kuwo.cn/anti.s?type=convert_url&rid=MUSIC_%s"
             "&format=mp3&response=url" % rid)
        r = http(u, {"Referer": "https://www.kuwo.cn/"})
        if r["status"] != 200:
            self.last_reason = "convert_url HTTP %s" % r["status"]
            return None
        url = r["body"].decode("utf-8", "replace").strip()
        if not url.startswith("http"):
            self.last_reason = "非直链回复：%s" % url[:60]
            return None
        ok, note, ext = verify_playable(url, "https://www.kuwo.cn/")
        if ok:
            return {"url": url, "quality": "128k", "ext": ext or "mp3", "note": note}
        self.last_reason = "verify 失败：" + note
        return None


# =============================================================== 酷狗（签名已自研；播放口需 token）
class Kugou:
    id = "kg"; name = "酷狗音乐"; kind = "platform"; homepage = "https://www.kugou.com/"
    QUALITY_RANK = {"flac": 1.0, "320k": 0.9, "128k": 0.6}
    SALT = "NVPh5oo715z5DIWAeQlhMDsWXXQV4hwt"
    DFID = "74a7fd31239b4df8a624e8ef94c45d70"

    @classmethod
    def sign(cls, params):
        s = "".join(f"{k}={params[k]}" for k in sorted(params) if k != "signature")
        return _md5(cls.SALT + s + cls.SALT)

    def search(self, title, singer):
        u = ("http://mobilecdn.kugou.com/api/v3/search/song?format=json&keyword=" +
             urllib.parse.quote(f"{title} {singer}") + "&page=1&pagesize=20&showtype=1")
        info = ((jload(http(u)) or {}).get("data") or {}).get("info") or []
        return [{"src": self.id, "title": x.get("songname", ""), "singer": x.get("singername", ""),
                 "album": x.get("album_name", ""),
                 "cover": (x.get("album_img") or "").replace("{size}", "480").replace("http://", "https://"),
                 "qualitys": ["128k"], "raw": {"hash": x.get("hash"), "album_id": x.get("album_id"),
                                               "pay_type": x.get("pay_type"), "dur": x.get("duration")}}
                for x in info]

    def resolve(self, item):
        """签名链路自研已通，但酷狗播放口 2024 起对匿名返回 err=30020（需登录 token）"""
        raw = item["raw"]
        mid = _randhex(32)
        ct = str(int(time.time() * 1000))
        for eid in (raw.get("hash"), raw.get("album_id")):
            if not eid: continue
            base = {"appid": "1014", "clienttime": ct, "clientver": "20000", "dfid": self.DFID,
                    "encode_album_audio_id": str(eid), "mid": mid, "platid": "4", "srcappid": "2919",
                    "token": "", "userid": "0", "uuid": mid}
            url = ("https://wwwapi.kugou.com/play/songinfo?" + urllib.parse.urlencode(base) +
                   "&signature=" + self.sign(base))
            r = http(url, {"Referer": "https://www.kugou.com/",
                           "Cookie": f"kg_mid={mid}; kg_dfid={self.DFID}; kg_dfid_collect={self.DFID}"})
            d = (jload(r) or {}).get("data") or {}
            u = d.get("play_url") or ""
            if u:
                ok, note, ext = verify_playable(u, "https://www.kugou.com/")
                if ok:
                    return {"url": u, "quality": "320k", "ext": ext or "mp3", "note": note}
            else:
                self.last_reason = f"err={d.get('err_code')}（需登录 token）"
        return None


# =============================================================== 注册表（可插拔）
ALL = [Migu, Bili, Netease, QQ, Kugou, Kuwo]
BY_ID = {c.id: c for c in ALL}


def instantiate():
    return {c.id: c() for c in ALL}


# =============================================================== 榜单（我们自己的规范化）
# topid 取自 QQ 音乐公开榜单；覆盖 热歌/飙升/新歌/风格/地区，用于把自有歌库撑起来
QQ_TOPLISTS = [
    ("26", "热歌榜", 300), ("4", "流行指数", 100), ("62", "飙升榜", 100),
    ("27", "新歌榜", 100), ("28", "网络歌曲榜", 100), ("5", "内地榜", 100),
    ("6", "港台榜", 100), ("3", "欧美榜", 100),
]


def qq_toplist(topid, name, limit=100):
    """榜单抓取（分页聚合版，2026-10-06）。
    为什么分页：云端（GitHub Actions 海外出口）实测被 QQ 风控降级——单次请求 song_num=300
    只回 50 条；本地家宽同请求回全量。分页 song_begin=0,50,100… 每页小请求 + 去重合并，
    云端也能凑回接近全量的榜单；本地则第一页就够、后续页去重后自然收敛。"""
    u0 = ("https://c.y.qq.com/v8/fcg-bin/fcg_v8_toplist_cp.fcg?topid=" + topid +
          "&format=json&page=detail&type=top&song_begin={begin}&song_num={num}")
    out, seen_mid = [], set()
    PAGE = 50
    for begin in range(0, max(limit, PAGE), PAGE):
        j = jload(http(u0.format(begin=begin, num=PAGE), {"Referer": "https://y.qq.com/"})) or {}
        songs = j.get("songlist") or []
        if not songs:
            break
        for x in songs:
            d = x.get("data") or {}
            mid = d.get("songmid")
            if not mid or mid in seen_mid:
                continue
            seen_mid.add(mid)
            albummid = d.get("albummid") or (d.get("album") or {}).get("mid") or ""
            singers = d.get("singer") or []
            out.append({"title": d.get("songname", ""),
                        "singer": "/".join(g.get("name", "") for g in singers),
                        "singers": [{"name": g.get("name"), "mid": g.get("mid")} for g in singers],
                        "album": d.get("albumname") or (d.get("album") or {}).get("name", ""),
                        "duration": d.get("interval"),
                        "cover": f"https://y.gtimg.cn/music/photo_new/T002R500x500M000{albummid}.jpg" if albummid else "",
                        "mid": mid, "albummid": albummid, "vid": d.get("vid") or "",
                        "rank": x.get("cur_count") or len(out) + 1})
            if len(out) >= limit:
                return {"topid": topid, "name": name, "count": len(out), "songs": out}
        if len(songs) < PAGE:      # 不足一页 = 已到底
            break
    return {"topid": topid, "name": name, "count": len(out), "songs": out}


def qq_singer_avatar(mid, size=300):
    """QQ 歌手头像（我们自己的规范化地址），size ∈ {150,300,500}"""
    return f"https://y.gtimg.cn/music/photo_new/T001R{size}x{size}M000{mid}.jpg" if mid else ""


def qq_hotkeys():
    j = jload(http("https://c.y.qq.com/splcloud/fcgi-bin/gethotkey.fcg",
                   {"Referer": "https://y.qq.com/"})) or {}
    return [{"k": h.get("k", "").strip(), "n": h.get("n")} for h in (j.get("data") or {}).get("hotkey", [])][:20]


# --------------------------------------------------------------- 分类（照大牌逻辑：语种/流派/主题/心情/场景）
# 标签体系取自 QQ 音乐官方歌单分类配置（实测可用，2026-10-05），数据自持：我们抓下来规范化成自己的 categories.json
DISS_TAG_CONF = ("https://c.y.qq.com/splcloud/fcgi-bin/fcg_get_diss_tag_conf.fcg?format=json"
                 "&inCharset=utf8&outCharset=utf-8&notice=0&platform=yqq.json&needNewCode=0")
DISS_BY_TAG = ("https://c.y.qq.com/splcloud/fcgi-bin/fcg_get_diss_by_tag.fcg?picmid=1&rnd=0.1&g_tk=5381"
               "&loginUin=0&hostUin=0&format=json&inCharset=utf8&outCharset=utf-8&notice=0"
               "&platform=yqq.json&needNewCode=0&categoryId={cid}&sortId={sort}&sin=0&ein={ein}")
DISS_SONGS = "https://u.y.qq.com/cgi-bin/musicu.fcg?data="


def _diss_songs_url(dissid, n=40):
    payload = {"comm": {"ct": 24, "cv": 0},
               "req": {"module": "music.srfDissInfo.DissInfo", "method": "CgiGetDiss",
                       "param": {"disstid": int(dissid or 0), "dirid": 0, "tag": 1,
                                 "song_begin": 0, "song_num": int(n), "userinfo": 0}}}
    return DISS_SONGS + urllib.parse.quote(json.dumps(payload, ensure_ascii=False))


def qq_diss_tags():
    """官方分类分组：[{group:语种, cats:[{name, id}]}...]（剔除「热门/全部」）"""
    import html as _html
    j = jload(http(DISS_TAG_CONF, {"Referer": "https://y.qq.com/"})) or {}
    out = []
    for g in ((j.get("data") or {}).get("categories") or []):
        name = g.get("categoryGroupName", "")
        if name in ("热门",):
            continue
        cats = [{"name": _html.unescape(i.get("categoryName", "")), "id": i.get("categoryId")}
                for i in (g.get("items") or []) if i.get("categoryId")]
        if cats:
            out.append({"group": name, "cats": cats})
    return out


def qq_diss_list(cid, sort=3, n=10):
    """某分类下最热门的歌单（dissid/名称/播放量）"""
    j = jload(http(DISS_BY_TAG.format(cid=cid, sort=sort, ein=max(n - 1, 0)),
                   {"Referer": "https://y.qq.com/"})) or {}
    return [{"dissid": x.get("dissid"), "name": x.get("dissname", ""),
             "play": x.get("listennum"), "pic": x.get("imgurl") or x.get("picurl") or ""}
            for x in ((j.get("data") or {}).get("list") or [])[:n]]


def qq_diss_songs(dissid, n=40):
    """歌单内的歌曲 → 规范化成与榜单一致的结构"""
    j = jload(http(_diss_songs_url(dissid, n), {"Referer": "https://y.qq.com/"})) or {}
    r = (j.get("req") or {}).get("data") or {}
    out = []
    for d in (r.get("songlist") or [])[:n]:
        d = d.get("data") if isinstance(d, dict) and isinstance(d.get("data"), dict) else d
        mid = d.get("albummid") or (d.get("album") or {}).get("mid") or ""
        singers = d.get("singer") or []
        out.append({"title": d.get("title") or d.get("songname", ""),
                    "singer": "/".join(g.get("name", "") for g in singers),
                    "album": (d.get("album") or {}).get("name", "") if isinstance(d.get("album"), dict) else d.get("album", ""),
                    "duration": d.get("interval"),
                    "cover": f"https://y.gtimg.cn/music/photo_new/T002R300x300M000{mid}.jpg" if mid else "",
                    "mid": d.get("mid") or d.get("songmid"), "albummid": mid})
    return out


# --------------------------------------------------------------- 真播放：为每首歌匹配"可直接播放的长效直链"
WY_OUTER = "https://music.163.com/song/media/outer/url?id=%s.mp3"


def wy_candidates(title, singer, limit=10):
    """网易云候选（未校验）"""
    kw = ("%s %s" % (title, singer)).strip()
    u = ("https://music.163.com/api/search/get?s=" + urllib.parse.quote(kw) +
         "&type=1&limit=%d&offset=0" % max(6, limit))
    j = jload(http(u, {"Referer": "https://music.163.com/"})) or {}
    out = []
    for s in (((j.get("result") or {}).get("songs")) or []):
        alb = s.get("album") or {}
        out.append({"id": s.get("id"), "name": s.get("name", ""),
                    "artist": "/".join(a.get("name", "") for a in (s.get("artists") or [])),
                    "dur": int((s.get("duration") or 0) / 1000),
                    "cover": (alb.get("picUrl") or "").replace("http://", "https://"),
                    "album": alb.get("name") or "",
                    # 搜索接口白送 originCoverType：0 未知 / 1 原曲 / 2 翻唱
                    "oct": int(s.get("originCoverType") or 0)})
    return out


def _tail_ok(longer, shorter):
    """longer 比 shorter 多出来的尾巴是不是"无害后缀"（纯英文/数字，如 rb、explicit、remastered）"""
    tail = longer[len(shorter):]
    return 0 < len(tail) <= 12 and all(ch.isascii() and (ch.isalnum()) for ch in tail)


def _title_close(nt, nm):
    """标题是否可视为同一首（容忍尾缀差异：'是非题rb'~'是非题'、'stormiiexplicit'~'stormii'）"""
    if not nt or not nm:
        return False
    if nt == nm:
        return True
    if len(nm) >= 2 and nt.startswith(nm) and _tail_ok(nt, nm):
        return True
    if len(nt) >= 2 and nm.startswith(nt) and _tail_ok(nm, nt):
        return True
    return False


def wy_match(title, singer, tries=12):
    """给一首歌找**实测能播**的网易云 id。返回 {id,name,artist,native,dur,tier} 或 None。

    旧版只试 5 个候选 → 大量"原版是 VIP/无版权(返回 HTML)，可播的 Live/正规版排在第 6+ 位"被漏掉；
    新版扩到 12 个候选，并加**质量闸门**，只接受一类：
      tier 0 = 标题相关 且 歌手吻合 → 原唱/正规版/Live
    **歌手对不上的（无论标题多像）一律拒绝**——实测"标题一致但歌手不同"会大量错配成同名翻唱
    （"倒数"→xjish、"honey"→桐生千弘、"我们的爱"→于潼），宁缺毋滥。"""
    cands = wy_candidates(title, singer, limit=max(10, tries + 4))
    if not cands:
        return None
    nt, ns = norm(title), norm(singer)

    def key(c):
        nm, ar = norm(c["name"]), norm(c["artist"])
        r = 0
        if nt and nm == nt:
            r -= 10
        elif nt and (nt in nm or nm in nt):
            r -= 5
        if ns and (ns in ar or ar in ns):
            r -= 7
        r += 3 * badness(c["name"]) + 2 * badness(c["artist"])
        return r

    rank = []
    for c in cands:
        nm, ar = norm(c["name"]), norm(c["artist"])
        t_exact = bool(nt) and _title_close(nt, nm)
        t_rel = bool(nt) and (t_exact or nt in nm or nm in nt)
        a_rel = bool(ns) and (ns in ar or ar in ns)
        if not (t_rel and a_rel):
            continue                      # 标题不相关 或 歌手对不上 → 拒绝
        if badness(c["name"]) >= 2 or badness(c["artist"]) >= 2:
            continue
        rank.append((key(c), len(c["name"]), c))
    rank.sort(key=lambda x: (x[0], x[1]))

    for _, _, c in rank[:max(1, tries)]:
        ok, note, ext = verify_playable(WY_OUTER % c["id"], "https://music.163.com/", 2048)
        if ok:
            c["native"] = bool(_title_close(nt, norm(c["name"])) and ns
                               and (ns in norm(c["artist"])))
            c["note"] = note
            c["tier"] = 0
            return c
    return None


# --------------------------------------------------------------- 歌词（LRC）与评论
def qq_lyric(mid):
    """QQ 官方歌词（LRC 原文 + 翻译）。mid 为 QQ songmid。"""
    if not mid:
        return None
    u = ("https://c.y.qq.com/lyric/fcgi-bin/fcg_query_lyric_new.fcg?songmid=" +
         urllib.parse.quote(str(mid)) + "&format=json&nobase64=1&g_tk=5381")
    j = jload(http(u, {"Referer": "https://y.qq.com/portal/player.html"})) or {}
    if j.get("retcode") != 0:
        return None
    ly = (j.get("lyric") or "").strip()
    if not ly:
        return None
    return {"lyric": ly, "trans": (j.get("trans") or "").strip()}


def wy_lyric(sid):
    """网易云歌词（LRC 原文 + 翻译），按网易云歌曲 id。"""
    if not sid:
        return None
    u = "https://music.163.com/api/song/lyric?id=%s&lv=1&kv=0&tv=-1" % sid
    j = jload(http(u, {"Referer": "https://music.163.com/"})) or {}
    lrc = ((j.get("lrc") or {}).get("lyric") or "").strip()
    if not lrc:
        return None
    return {"lyric": lrc, "trans": ((j.get("tlyric") or {}).get("lyric") or "").strip()}


def wy_comments(sid, n=20):
    """网易云热评 + 最新评论（真数据）"""
    if not sid:
        return None
    u = "https://music.163.com/api/v1/resource/comments/R_SO_4_%s?limit=%d&offset=0" % (sid, n)
    j = jload(http(u, {"Referer": "https://music.163.com/song?id=%s" % sid})) or {}
    if j.get("code") != 200:
        return None

    def pack(lst):
        out = []
        for c in lst or []:
            txt = (c.get("content") or "").replace("\n", " ").replace("\r", " ").strip()
            if not txt:
                continue
            ts = c.get("time") or 0
            out.append({"u": (c.get("user") or {}).get("nickname", ""), "c": txt[:300],
                        "l": c.get("likedCount") or 0,
                        "d": time.strftime("%Y-%m-%d", time.localtime(ts / 1000)) if ts else ""})
        return out
    hot, new = pack(j.get("hotComments")), pack(j.get("comments"))
    if not hot and not new:
        return None
    return {"total": j.get("total") or 0, "hot": hot[:12], "new": new[:12]}


# =============================================== 多源选曲（★ 2026-10-06 核心）
# 主人两个硬要求都落在这个函数里：
#   ① 「原唱必须排第一，不能出现翻唱」→ 歌手吻合 + badness 降权 + 原唱标记三重判据
#   ② 「VIP 也想办法能到」            → 网易云拿不到时按优先链路回退到酷我/咪咕/B站
#
# 顺序为什么这样排（实测依据）：
#   wy  → 音质最好、有 oct 原唱标记、歌词最全；**但 VIP/付费歌拿不到**（4515+text/html）
#   kw  → ★ 匿名唯一稳出真音频的大平台（实测 5/6，含周杰伦等 VIP 大户）；音质 128k
#   mg  → 咪咕匿名只出免费曲，VIP 回 200002；作补充
#   bili→ 兜底最广（翻唱/稀缺资源都有），但**必须靠 UP 主标题判原唱**，误配风险高 → 放最后
# ★ 2026-10-07 修：原写 "mg"，但 Migu.id = "migu"（见 class Migu），
#   srcs.get("mg") 恒为 None → 咪咕这一路回退从来没有生效过。
MULTI_ORDER = ("wy", "kw", "migu", "bili")


def _score_cand(c, nt, ns, want_dur_ms=0, strict=False):
    """给候选打分（越小越好）。判据全部来自实测血案：
      · 歌手必须吻合（否则"同名不同人"错配 —— 历史的 倒数/xjish、honey/桐生千弘）
      · ★ HARD_BAD 直接毙 —— 实测酷我第一条常是伴奏/片段/DJ 版，候选池里没干净的
      · ★ SOFT_BAD 8 分/词重罚（原 4 分，实测罚不动）—— dj/remix/演唱会/翻唱
      · 原唱标记 oct=1 加分、oct=2 重罚 —— 网易云白送，权威判据
      · 时长差越小越好 —— 防"试听片段"和"串烧合集"
      · ★ 标题比目标长太多 → 大概率是另一首（情歌 ≠ 情歌没有告诉你）
    """
    nm, ar = norm(c.get("title")), norm(c.get("singer"))
    if not nt or not nm:
        return None
    if not _title_close(nt, nm) and not (nt in nm or nm in nt):
        return None                                   # 标题不相关 → 直接毙
    if ns and not (ns in ar or ar in ns):
        return None                                   # ★ 歌手对不上 → 直接毙（宁缺毋滥）
    # ★ 硬毙闸门①：标题+专辑里出现「伴奏/片段/谱/教学/宣传片/DJ/Remix/非音乐」
    #   → 这不是这首歌（或其原唱版本）。宁可这歌没有直链，也不拿脏的顶。
    blob = "%s %s" % (c.get("title") or "", c.get("album") or "")
    if hard_bad(blob):
        c["__rej"] = "HARD_BAD"
        return None
    # ★ 硬毙闸门②：时长异常。
    #   实测漏网案例：`Always Online (2025 JJ20 F…` 只有 121s、`唯一 (TRAP Remix)` 只有 50s ——
    #   全是试听片段/副歌剪辑，端上播出来是半首，比没有还糟。原来只 +6 分罚不动，改硬毙。
    #   ⚠️ 四个适配器把时长放在不同位置（顶层 dur / raw.dur / "mm:ss" 字符串），
    #      必须统一走 dur_s 归一化成秒，否则时长闸门形同虚设。
    d = dur_s(c.get("dur") or (c.get("raw") or {}).get("dur"))
    if d:
        if d < MIN_CAND_DUR:
            c["__rej"] = "TOO_SHORT"
            return None
        if want_dur_ms:
            wd = int(want_dur_ms) / 1000.0
            if abs(d - wd) > max(DUR_TOL_ABS, wd * DUR_TOL_REL):
                c["__rej"] = "DUR_MISMATCH"
                return None
    # ★ 硬毙闸门③：标题长度差过大 → 大概率是「另一首同前缀的歌」
    #   （情歌 ≠ 情歌没有告诉你；茶花开了，该回家了 ≠ 茶花开了）
    #   容差 4 个字：「晴天」→「晴天 (深情版)」(差 3) 照过，差 6 的串名拦下。
    #   strict 下收紧到 3（曲库多源采集用，见下）。
    if abs(len(nm) - len(nt)) > (3 if strict else 4):
        c["__rej"] = "TITLE_FAR"
        return None
    # ★ strict 模式（曲库多源采集专用）：任何 SOFT_BAD 版本标记都直接毙。
    #   为什么必须这么狠：非网易曲目拿不到 originCoverType，身份完全靠标题判断。
    #   一旦放进 Live/翻唱/DJ 版，端上就会「显示《情歌》、播出来是演唱会版」——
    #   这比这首歌暂时没流糟糕得多（主人：「宁可不治也不能错治」）。
    #   ⚠️ 存量 streams.json 生成仍走 strict=False，行为不变。
    if strict and soft_bad(blob):
        c["__rej"] = "SOFT_BAD_STRICT"
        return None
    r = 0
    r += 8 * soft_bad(blob)                           # 劣质变体重罚（比原来的 4 分/词翻倍）
    r += 2 * badness(c.get("singer"))
    oct_ = c.get("oct")
    if oct_ == 1:
        r -= 8                                        # 网易云标了"原曲" → 大加分
    elif oct_ == 2:
        r += 12                                       # 网易云标了"翻唱" → 重罚
    if want_dur_ms and d:                             # 过了硬毙闸门，剩下的按时长贴近度排序
        r += min(abs(d - int(want_dur_ms) / 1000.0), DUR_TOL_ABS) * 0.4
    r += len(nm) * 0.01                               # 同分取短标题（更贴近原始曲名）
    return r


def _long_id(sid, raw, c):
    """各源的「长效 id」藏在哪 —— 端上就拿这个 id + src 自己拼直链（见 kernel stRec/srcUrl）。

    ★ 2026-10-07 血案：原来只认 `raw.rid`（酷我字段），结果**咪咕这一路 id 恒为 None**，
      多源采集整批落不了库（实测 60 条候选 → 新增 0 条）。各源的真实字段：
        kw   → raw.rid          （酷我 MUSIC_<rid>）
        migu → raw.contentId    （端上 listenV2?contentId=<id>）
        bili → raw.bvid         （B 站视频号）
        kg   → raw.hash
        wy   → c.id             （网易 songId）
    """
    if sid == "kw":
        return raw.get("rid")
    if sid == "migu":
        return raw.get("contentId") or raw.get("copyrightId")
    if sid == "bili":
        return raw.get("bvid")
    if sid == "kg":
        return raw.get("hash")
    return raw.get("rid") or c.get("id")


def multi_match(title, singer, dur_ms=0, order=MULTI_ORDER, per_src=8, want_try=3, strict=False):
    """多源找**实测能播**的直链。

    strict=False（默认，存量行为）：HARD_BAD 硬毙 + SOFT_BAD 8 分/词降权。
      —— 用于 build.py 给自有歌库补流：候选池小，能补上一条就比没有强。
    strict=True（★ 曲库多源采集）：SOFT_BAD 也硬毙，只收「标题完全干净的原版」。
      —— 用于把 QQ/酷狗/咪咕 的曲目并进曲库：身份只能靠标题判断，宁缺毋滥。

    返回 {id, src, url, quality, ext, name, artist, cover, album, dur, oct, note} 或 None。
      · id   = **长效 id**（wy songId / kw rid / migu contentId），不存临时直链（带签名会过期）
      · src  = wy / kw / migu / bili —— 调用方落库时必须一起存，端上按它拼直链
      · cover/album/dur = ★ 2026-10-07 新增：多源曲目要进曲库必须自带封面，
        否则 load_candidates 会判「字段不全」整批丢弃

    调用方拿到 id 后自己拼：
      wy   → https://music.163.com/song/media/outer/url?id=<id>.mp3
      kw   → 需运行时调 antiserver（见 Kuwo.resolve）
      migu → app.pd.nf.migu.cn listenV2?contentId=<id>&resourceType=2
    """
    nt, ns = norm(title), norm(singer)
    srcs = instantiate()
    tried = []

    # 网易云优先：先拿官方原唱标记（oct），再选曲
    wy = srcs.get("wy")
    if wy and "wy" in order:
        try:
            cands = wy_candidates(title, singer, limit=max(8, per_src + 4))
            ranked = []
            for c in cands:
                sc = _score_cand(c, nt, ns, dur_ms, strict)
                if sc is not None:
                    ranked.append((sc, c))
            ranked.sort(key=lambda x: x[0])
            for _, c in ranked[:want_try]:
                ok, note, ext = verify_playable(WY_OUTER % c["id"], "https://music.163.com/", 2048)
                tried.append(("wy", c.get("name"), note))
                if ok:
                    return {"id": c["id"], "src": "wy", "url": WY_OUTER % c["id"],
                            "quality": "128k", "ext": ext or "mp3",
                            "name": c.get("name"), "artist": c.get("artist"),
                            "cover": c.get("cover") or "", "album": c.get("album") or "",
                            "dur": dur_s(c.get("dur")),
                            "oct": c.get("oct") or 0, "note": note}
        except Exception as e:
            tried.append(("wy", "-", "异常 %s" % e))

    # 其余源依次回退
    for sid in order:
        if sid == "wy":
            continue
        ad = srcs.get(sid)
        if not ad:
            continue
        try:
            items = ad.search(title, singer) or []
        except Exception as e:
            tried.append((sid, "-", "搜索异常 %s" % e))
            continue
        ranked = []
        for c in items:
            sc = _score_cand(c, nt, ns, dur_ms, strict)
            if sc is not None:
                ranked.append((sc, c))
        ranked.sort(key=lambda x: x[0])
        for _, c in ranked[:want_try]:
            try:
                r = ad.resolve(c)
            except Exception as e:
                r = None
                tried.append((sid, c.get("title"), "解析异常 %s" % e))
            if r:
                raw = c.get("raw") or {}
                return {"id": _long_id(sid, raw, c),
                        "src": sid, "url": r.get("url"), "quality": r.get("quality"),
                        "ext": r.get("ext"), "name": c.get("title"), "artist": c.get("singer"),
                        # ★ 2026-10-07：多源曲目要落进曲库，必须自带给封面/专辑/时长
                        #   （load_candidates 会把没有 cover 的记录判成「字段不全」丢掉）
                        "cover": c.get("cover") or "", "album": c.get("album") or "",
                        "dur": dur_s(raw.get("dur") or c.get("dur")),
                        "oct": 0, "note": r.get("note")}
            tried.append((sid, c.get("title"), getattr(ad, "last_reason", "无直链")))
    return None


if __name__ == "__main__":
    import sys
    title, singer = (sys.argv[1:3] + ["走在冷风中", "刘思涵"])[:2]
    print(f"自研适配器自检：{title} / {singer}")
    srcs = instantiate()
    for sid in ("migu", "bili", "wy", "qq", "kg", "kw"):
        s = srcs[sid]
        try:
            t0 = time.time()
            items = s.search(title, singer)
            pick_it = pick(items, title, singer, lambda x: x["title"], lambda x: x["singer"])
            if not pick_it:
                print(f"  {s.name:<8} 搜索{len(items):>3}条 无匹配")
                continue
            t1 = time.time()
            r = s.resolve(pick_it)
            note = (r or {}).get("note") or getattr(s, "last_reason", "无直链")
            print(f"  {s.name:<8} 搜索{len(items):>3}条 → {pick_it['title'][:20]:<22} "
                  f"{'✓ ' + str(r.get('quality')) if r else '✗'} {int((time.time()-t1)*1000)}ms  {note}")
        except Exception as e:
            print(f"  {s.name:<8} 异常 {type(e).__name__}: {e}")
    print("—— 多源选曲 multi_match ——")
    m = multi_match(title, singer)
    print(" ", m if not m else {k: (v[:70] if k == "url" else v) for k, v in m.items()})
