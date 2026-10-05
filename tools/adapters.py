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
             "八音盒", "口琴", "萨克斯", "二胡", "古筝", "铃声版", "字幕", "动态歌词", "完整版")


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
ALL = [Migu, Bili, Netease, QQ, Kugou]
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
    u = ("https://c.y.qq.com/v8/fcg-bin/fcg_v8_toplist_cp.fcg?topid=" + topid +
         f"&format=json&page=detail&type=top&song_begin=0&song_num={limit}")
    j = jload(http(u, {"Referer": "https://y.qq.com/"})) or {}
    out = []
    for x in (j.get("songlist") or []):
        d = x.get("data") or {}
        mid = d.get("albummid") or (d.get("album") or {}).get("mid") or ""
        singers = d.get("singer") or []
        out.append({"title": d.get("songname", ""),
                    "singer": "/".join(g.get("name", "") for g in singers),
                    "singers": [{"name": g.get("name"), "mid": g.get("mid")} for g in singers],
                    "album": d.get("albumname") or (d.get("album") or {}).get("name", ""),
                    "duration": d.get("interval"),
                    "cover": f"https://y.gtimg.cn/music/photo_new/T002R500x500M000{mid}.jpg" if mid else "",
                    "mid": d.get("songmid"), "albummid": mid, "vid": d.get("vid") or "",
                    "rank": x.get("cur_count") or len(out) + 1})
    return {"topid": topid, "name": name, "count": len(out), "songs": out}


def qq_singer_avatar(mid, size=300):
    """QQ 歌手头像（我们自己的规范化地址），size ∈ {150,300,500}"""
    return f"https://y.gtimg.cn/music/photo_new/T001R{size}x{size}M000{mid}.jpg" if mid else ""


def qq_hotkeys():
    j = jload(http("https://c.y.qq.com/splcloud/fcgi-bin/gethotkey.fcg",
                   {"Referer": "https://y.qq.com/"})) or {}
    return [{"k": h.get("k", "").strip(), "n": h.get("n")} for h in (j.get("data") or {}).get("hotkey", [])][:20]


if __name__ == "__main__":
    import sys
    title, singer = (sys.argv[1:3] + ["稻香", "周杰伦"])[:2]
    print(f"自研适配器自检：{title} / {singer}")
    srcs = instantiate()
    for sid in ("migu", "bili", "wy", "qq", "kg"):
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
