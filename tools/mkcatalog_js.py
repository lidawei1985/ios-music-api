# -*- coding: utf-8 -*-
"""曲库 → 可被 <script src> 直接加载的 JS 分块（随包内置，离线可用）。

为什么不再纯靠 jsDelivr：
  实测本机到 cdn.jsdelivr.net 只有 1~8 KB/s（走代理才 440 KB/s）——中国网络下不可用。
  靠它拉 14MB 搜索索引 = 每次开 App 都得等几分钟，与「流畅」直接冲突。
  所以把曲库**打进 App 包内**，用 <script src> 相对路径加载（file:// 下 script/img 子资源可用，
  fetch/XHR 才会被拦），换来「搜索零等待、断网可用」。CDN 保留为可选增量源。

产物（全部扁平命名，放 app.html 同目录即可）：
  cat-manifest.js          window.__CATMAN = {...}
  cat-idx-NN.js            window.__CATIDX[NN] = "<纯文本分块>"
  cat-sh-NN.js             window.__CATSH[g*10 .. g*10+9] = [ ... ]   （每包 10 片）

为什么索引与分片分开打包：索引必须在开 App 后尽快到位（搜索要用），
分片（封面/时长）只在「全部歌曲」页按需增量取。

用法：
  python tools/mkcatalog_js.py                 # 输出到 cloud/data/catalog/js/
  python tools/mkcatalog_js.py --out <目录>     # 指定输出目录
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
CLOUD = os.path.dirname(HERE)
CAT = os.path.join(CLOUD, "data", "catalog")
SH_PER_PACK = 10


def arg(flag, default):
    if flag in sys.argv:
        i = sys.argv.index(flag)
        if i + 1 < len(sys.argv):
            return sys.argv[i + 1]
    return default


OUT = os.path.abspath(arg("--out", os.path.join(CAT, "js")))


def wr(name, text):
    p = os.path.join(OUT, name)
    with open(p, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    return os.path.getsize(p)


def main():
    os.makedirs(OUT, exist_ok=True)
    for old in os.listdir(OUT):
        if old.startswith("cat-") and old.endswith(".js"):
            os.remove(os.path.join(OUT, old))

    man = json.load(open(os.path.join(CAT, "manifest.json"), encoding="utf-8"))
    files = []

    # ① 清单
    files.append(("cat-manifest.js",
                  "window.__CATMAN=" + json.dumps(man, ensure_ascii=False, separators=(",", ":")) + ";\n"))

    # ② 索引分块（一行一条的纯文本，原样进字符串；json.dumps 会把 \u0001 转义成合法 JS 转义）
    idx = (man.get("idx") or {})
    chunks = idx.get("chunks") or []
    for i, c in enumerate(chunks):
        txt = open(os.path.join(CAT, c), encoding="utf-8").read()
        files.append(("cat-idx-%02d.js" % i,
                      "window.__CATIDX=window.__CATIDX||[];"
                      "window.__CATIDX[%d]=%s;\n" % (i, json.dumps(txt, ensure_ascii=False))))

    # ③ 分片（每 SH_PER_PACK 片合成一个文件，避免 130+ 个文件）
    shards = man.get("shards") or []
    for g in range(0, len(shards), SH_PER_PACK):
        grp = shards[g:g + SH_PER_PACK]
        parts = ["window.__CATSH=window.__CATSH||{};"]
        for k, s in enumerate(grp):
            arr = json.load(open(os.path.join(CAT, s["file"]), encoding="utf-8"))
            parts.append("window.__CATSH[%d]=%s;"
                         % (g + k, json.dumps(arr, ensure_ascii=False, separators=(",", ":"))))
        files.append(("cat-sh-%02d.js" % (g // SH_PER_PACK), "".join(parts) + "\n"))

    tot = 0
    print("输出目录:", OUT)
    for n, t in files:
        sz = wr(n, t)
        tot += sz
        if not (n.startswith("cat-sh-") and (int(n[7:9]) % 4)):
            print("  %-18s %8.2f MB" % (n, sz / 1048576))
    print("  ... 分片包共 %d 个" % len([1 for n, _ in files if n.startswith("cat-sh-")]))
    print("合计 %d 个文件 / %.1f MB" % (len(files), tot / 1048576))

    # 自检：生成的 JS 必须能被解析，且关键全局名对得上
    print("自检：文件数 %d ｜ 索引块 %d ｜ 分片 %d ｜ 曲目 %d"
          % (len(files), len(chunks), len(shards), man.get("count") or 0))


if __name__ == "__main__":
    main()
