# -*- coding: utf-8 -*-
"""只跑「真播放链路」：可播直链映射 + 歌词 + 评论（用现有 data/*.json 作输入，不重抓榜单/分类）"""
import json, os, sys, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import build as B

DATA = B.DATA
charts = B.load(os.path.join(DATA, "charts.json"), {})
cats = B.load(os.path.join(DATA, "categories.json"), {})
hero = B.load(os.path.join(DATA, "hero.json"), {})
lib = B.load(os.path.join(DATA, "library.json"), {})

acc = B._collect_songs(charts, cats, hero, lib)
print("曲目去重后 %s 首" % len(acc))
t0 = time.time()

streams = B.build_streams(charts, cats, hero, lib)
B.save(os.path.join(DATA, "streams.json"), streams, indent=None)

lyr = B.build_lyrics(acc, streams)
B.save(os.path.join(DATA, "lyrics.json"), lyr, indent=None)

com = B.build_comments(acc, streams)
B.save(os.path.join(DATA, "comments.json"), com, indent=None)

print("\n完成 用时 %.0fs" % (time.time() - t0))
print("streams %s/%s = %s%% | lyrics %s | comments %s"
      % (streams["hit"], streams["count"], streams["rate"], lyr["count"], com["count"]))
