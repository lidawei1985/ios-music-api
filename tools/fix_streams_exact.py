# -*- coding: utf-8 -*-
"""streams.json 纠偏：只保留「原唱精确命中」的映射。

背景（2026-10-05 真机反馈）：播《如诗一般的形容妳》出来的是《连名带姓》——
就近匹配(native=0)把翻唱/伴奏/完全无关的歌拿来充数，全库 3259 条里错配 1015 条(31%)。
表现 = 播错歌 + 歌词对不上 + 音质差。

铁律：宁缺毋滥。映射不上的歌宁可提示"暂无可用音源"，绝不放错的。
"""
import json, re, os, sys, time

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SRC = os.path.join(ROOT, "cloud", "data", "streams.json")

def norm(s):
    if not s: return ""
    return re.sub(r"[ \"\t\r\n\-_（）()【】·,.，。'\"!！?？~～&]", "", str(s).lower())

def main():
    d = json.load(open(SRC, encoding="utf-8"))
    mp = d.get("map") or {}
    total = len(mp)
    keep, dropped_native, dropped_mismatch = {}, 0, 0
    for k, v in mp.items():
        title = k.split("|")[0]
        if norm(v.get("n", "")) != norm(title):
            if v.get("native") == 1: dropped_native += 1
            else: dropped_mismatch += 1
            continue
        keep[k] = v
    out = {
        "updated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "count": total,
        "hit": len(keep),
        "rate": round(len(keep) / total, 4) if total else 0,
        "native": sum(1 for v in keep.values() if v.get("native") == 1),
        "miss": total - len(keep),
        "note": "只保留原唱精确命中（映射歌名必须与目标歌名一致）；配不到的不映射，宁缺毋滥",
        "map": keep,
    }
    json.dump(out, open(SRC, "w", encoding="utf-8"), ensure_ascii=False, separators=(",", ":"))
    print("原映射 %d 条 → 保留精确命中 %d 条（误标原唱但歌名不符 %d，就近匹配剔除 %d）"
          % (total, len(keep), dropped_native, dropped_mismatch))
    print("命中率 %.1f%%" % (out["rate"] * 100))
    # 同步 CDN 发布目录
    cdn = os.path.join(ROOT, "cloud", "_cdn", "data", "streams.json")
    if os.path.isdir(os.path.dirname(cdn)):
        import shutil; shutil.copyfile(SRC, cdn); print("已同步 →", cdn)

if __name__ == "__main__":
    main()
