#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 data/<n>.json 转成 data/<n>.js（window.__DWG_DB_<NAME>=...）。

★ 为什么必须有这一份（2026-10-06 定案）：
   端上要「云端数据热更到手机」，但 iOS 的 WebView 在 file:// 页面下
   **拦截 fetch / XMLHttpRequest，只放行 <script> / <img> 这类子资源**。
   原来 kernel 的 fetchLive() 用 fetch 拉 data/<n>.json —— 在 iOS 上必然全 null，
   等于「云端数据永远热更不上手机」，只能靠重新出包。
   所以云端要同时提供 <script> 能吃的 .js 版本（与 data/assets/*.js 同一套路）。
   内容布局与 assets 的 .js 保持一致：window.__DWG_DB_POOL={...};

★ 与 .json 的关系：.json 仍是权威源（CI/工具链读写它）；
   .js 只是给端上 <script> 注入用的镜像，由本脚本在每轮发布前重新生成。
"""
import json, os, sys, time

CD = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(CD, "data")

# 端上会实时热更的（体积小、会变）。其余大文件仍随包内置，不在这里生成也没关系，
# 但统一生成成本极低，且方便以后按需扩展。
NAMES = ["pool", "charts", "library", "artists", "mv", "hero",
         "categories", "streams", "lyrics", "comments"]


def main():
    only = set(sys.argv[1:]) or None
    made, total = [], 0
    for n in NAMES:
        if only and n not in only:
            continue
        src = os.path.join(DATA, n + ".json")
        if not os.path.exists(src):
            print("  skip %s（无 json）" % n)
            continue
        with open(src, encoding="utf-8") as fh:
            body = json.load(fh)
        dst = os.path.join(DATA, n + ".js")
        tmp = dst + ".tmp"
        # 紧凑分隔符：这些文件动辄几百 KB，空格换行全是白流量。
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write("window.__DWG_DB_%s=" % n.upper())
            json.dump(body, fh, ensure_ascii=False, separators=(",", ":"))
            fh.write(";")
        os.replace(tmp, dst)          # 原子替换，避免端上读到半个文件
        sz = os.path.getsize(dst)
        total += sz
        made.append((n, sz))
    for n, sz in made:
        print("  %-12s -> data/%s.js  %d B" % (n, n, sz))
    print("[mkdata_js] 生成 %d 个，共 %.1f MB" % (len(made), total / 1048576))
    return 0


if __name__ == "__main__":
    sys.exit(main())
