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
    bad = []
    if not order:
        bad.append("没有任何可用源（order 为空）")
    if l.get("count", 0) < 20:
        bad.append("自有歌库过小(<20)")
    if not any(x.get("count", 0) > 50 for x in c.get("lists", [])):
        bad.append("榜单全空")
    if bad:
        sys.exit("FATAL: " + "; ".join(bad) + " —— 拒绝发布")
    print("OK")


if __name__ == "__main__":
    main()
