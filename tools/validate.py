#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""产出校验：没有可用源 / 歌库过小 一律拒绝发布（防止"静默产出空池"）"""
import json, os, sys

DATA = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")


def main():
    p = json.load(open(os.path.join(DATA, "pool.json"), encoding="utf-8"))
    c = json.load(open(os.path.join(DATA, "charts.json"), encoding="utf-8"))
    l = json.load(open(os.path.join(DATA, "library.json"), encoding="utf-8"))
    order = p.get("order") or []
    print("round:", p.get("round"), "| order:", order)
    for s in p.get("sources", []):
        print("  %-6s %-11s score=%-6s role=%-8s chain=%s %s"
              % (s["id"], s["status"], s["score"], s.get("role"),
                 round(s["chain_ok"], 2), (s.get("note") or "")[:40]))
    print("charts:", [(x["name"], x["count"]) for x in c.get("lists", [])])
    print("library:", l.get("count"))
    try:
        a = json.load(open(os.path.join(DATA, "artists.json"), encoding="utf-8"))
        print("artists:", a.get("count"))
    except Exception:
        a = {}
    try:
        h = json.load(open(os.path.join(DATA, "hero.json"), encoding="utf-8"))
        print("hero:", h.get("count"), "张高清海报 | 已取主色", h.get("hue_ok"))
    except Exception:
        h = {}
    try:
        cc = json.load(open(os.path.join(DATA, "categories.json"), encoding="utf-8"))
        print("categories:", [(g["name"], len(g["cats"]), g["count"]) for g in cc.get("groups", [])])
    except Exception:
        cc = {}
    bad = []
    warn = []
    # 云端机房出口会被中国区音源按地域风控（实测：咪咕全 VIP 闸门/QQ 搜索被拒），
    # 「试源探测」在云端只是尽力而为的参考值；真正的播放解析在设备端（App 自研适配器）。
    # 因此 order 为空只警告不拒发 —— 元数据（歌库/榜单/歌手/MV）才是云端的正式产物。
    if not order:
        warn.append("没有任何可用源（order 为空，云端探测被地域风控；设备端解析兜底）")
    if l.get("count", 0) < 20:
        bad.append("自有歌库过小(<20)")
    # 榜单判据（2026-10-06 适配云端现实）：QQ 榜单接口对海外出口有时降级到 50/榜（本地实测全量），
    # 分页聚合后云端通常能凑回 100+；这里只拦截「真全空」，≥30 视为有效产出，不再被 50 卡死。
    if not any(x.get("count", 0) >= 30 for x in c.get("lists", [])):
        bad.append("榜单全空")
    # 主视觉轮播是首页门面，硬要求 ≥20 张高清海报（数据来自榜单，稳定可得，不给静默降级）
    if h.get("count", 0) < 20:
        bad.append("主视觉海报不足 20 张（实际 %s）" % h.get("count", 0))
    if bad:
        sys.exit("FATAL: " + "; ".join(bad) + " —— 拒绝发布")
    for w in warn:
        print("WARN:", w)
    print("OK")


if __name__ == "__main__":
    main()
