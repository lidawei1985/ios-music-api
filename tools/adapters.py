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


# 劣质变体关键词：命中即降权（保证"优质优先"挑到正规原曲，而非翻唱/剪辑/二次创作）
BAD_WORDS = ("翻唱", "cover", "伴奏", "remix", "remix版", "dj", "串烧", "铃声", "片段", "剪辑",
             "纯音乐", "钢琴", "吉他", "教程", "教学", "简谱", "合唱", "现场", "live", "演唱会",
             "私藏", "歌单", "合集", "mv", "无损音乐馆", "治愈", "睡前", "助眠", "慢速", "加速",
             "八音盒", "口琴", "萨克斯", "二胡", "古筝", "铃声版", "字幕", "动态歌词", "完整版",
             "鼓谱", "曲谱", "琴谱", "乐谱", "动态鼓谱", "谱", "示范", "伴奏版", "和声", "翻弹",
             # ★★★ 2026-10-07 补充：**非音乐内容**（英语听力 / 考试真题 / 有声书 / 课文朗读）
             #   来由：2026-10-06 深夜 harvest 被自己的发布前体检拦下，抽样坏例子是
             #     「奥巴马会见格鲁吉亚总统(1/—英语听力；第三期 2006年6月真题 —英语听力」
             #   这类内容既不是歌，录音码率又低，混进库会同时压低"热度成色"和"音频可播率"，
             #   属于两头都踩雷的脏数据 —— 必须在**入库前**就挡掉，而不是等体检事后拦。
             "听力", "真题", "英语", "四级", "六级", "雅思", "托福", "单词", "音标",
             "课文", "朗读", "背诵", "有声书", "有声小说", "评书", "相声", "讲座",
             "教材", "试卷", "考题", "口语", "语法", "课件")


def badness(text):
    t = (text or "").lower()
    return sum(1 for w in BAD_WORDS if w in t)


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
            out.append({"src": self.id, "title": x.get("name", ""),
                        "singer": "/".join(s.get("name", "") for s in x.get("singers", [])),
                        "album": ((x.get("albums") or [{}])[0] or {}).get("name", ""),
                        "cover": img.replace("http://", "https://"),
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
            out.append({"src": self.id,
                        "title": re.sub(r"&nbsp;?", " ", x.get("SONGNAME") or "").strip(),
                        "singer": re.sub(r"&nbsp;?", " ", x.get("ARTIST") or "").strip(),
                        "album": re.sub(r"&nbsp;?", " ", x.get("ALBUM") or "").strip(),
                        "cover": "", "qualitys": ["128k"],
                        "raw": {"rid": rid, "dur": x.get("DURATION") or 0,
                                "fmt": x.get("FORMATS") or ""}})
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
    return [{"id": s.get("id"), "name": s.get("name", ""),
             "artist": "/".join(a.get("name", "") for a in (s.get("artists") or [])),
             "dur": int((s.get("duration") or 0) / 1000)}
            for s in (((j.get("result") or {}).get("songs")) or [])]


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
MULTI_ORDER = ("wy", "kw", "mg", "bili")


def _score_cand(c, nt, ns, want_dur_ms=0):
    """给候选打分（越小越好）。判据全部来自实测血案：
      · 歌手必须吻合（否则"同名不同人"错配 —— 历史的 倒数/xjish、honey/桐生千弘）
      · badness（dj/cover/伴奏/live…）重罚 —— 酷我搜索第一条常是 DJ 版
      · 原唱标记 oct=1 加分、oct=2 重罚 —— 网易云白送，权威判据
      · 时长差越小越好 —— 防"试听片段"和"串烧合集"
    """
    nm, ar = norm(c.get("title")), norm(c.get("singer"))
    if not nt or not nm:
        return None
    if not _title_close(nt, nm) and not (nt in nm or nm in nt):
        return None                                   # 标题不相关 → 直接毙
    if ns and not (ns in ar or ar in ns):
        return None                                   # ★ 歌手对不上 → 直接毙（宁缺毋滥）
    r = 0
    r += 4 * badness(c.get("title"))
    r += 2 * badness(c.get("singer"))
    oct_ = c.get("oct")
    if oct_ == 1:
        r -= 8                                        # 网易云标了"原曲" → 大加分
    elif oct_ == 2:
        r += 12                                       # 网易云标了"翻唱" → 重罚
    d = c.get("dur") or 0
    if want_dur_ms and d:
        diff = abs(int(d) - int(want_dur_ms)) / 1000.0
        if diff > 25:
            r += 6                                    # 时长差太多 → 大概率不是同一版
        r += min(diff, 25) * 0.2
    r += len(nm) * 0.01                               # 同分取短标题（更贴近原始曲名）
    return r


def multi_match(title, singer, dur_ms=0, order=MULTI_ORDER, per_src=8, want_try=3):
    """多源找**实测能播**的直链。返回 {id, src, url, quality, ext, name, artist, oct} 或 None。

    ★ 只返回**长效 id（wy id / kw rid）**给调用方存库，不存临时直链（带签名会过期）。
    调用方拿到 id 后自己拼：
      wy → https://music.163.com/song/media/outer/url?id=<id>.mp3
      kw → 需运行时调 antiserver（见 Kuwo.resolve）
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
                sc = _score_cand(c, nt, ns, dur_ms)
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
            sc = _score_cand(c, nt, ns, dur_ms)
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
                return {"id": (c.get("raw") or {}).get("rid") or c.get("id"),
                        "src": sid, "url": r.get("url"), "quality": r.get("quality"),
                        "ext": r.get("ext"), "name": c.get("title"), "artist": c.get("singer"),
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
