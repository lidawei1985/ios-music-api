#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""verify_skey.py —— 「封面/播放映射能不能对上」的唯一契约回归测试。

★★ 为什么必须有（这是「沉淀白做」的复发入口）：
   端上 app.html 用 skey(歌名, 歌手) 去查 assets/cv.json 的 map；
   采集器 localize.py 用 _js_skey(歌名, 歌手) 去写这份 map。
   两边**必须逐字符一致** —— 只要有一边多删一个符号、少截一个字符，
   键就全部错位：图都下好了，端上一张也查不到（封面整片回落上游 URL）。
   这类 bug 不会报错、不会崩，只会"看起来没生效"，极难排查。

   历史踩过的坑：
     · 一边按「归一化后截断」、一边按「截断后归一化」→ 全错
     · JS 的 slice() 按 UTF-16 码元、Python 的 [:n] 按码点 →
       标题里一旦有 emoji / 非 BMP 汉字（𠮷、🕯）就错位（实测曲库当前 0 命中，属潜伏）
   所以：契约不能靠"我们记得同步"，必须靠这个脚本在 CI 里天天跑。

做法：
   不复制粘贴两边的实现（那样改歪了测试也跟着歪），而是
   **直接从 kernel/app.html 里抽出真实的 norm / cpslice / skey 三行**，
   用 node 跑一批刁难用例，再和 localize.py 的 _js_skey 逐字符比对。
   任何一边改了，这里立刻红。

用法：
  python tools/verify_skey.py            # 0=一致，1=不一致
  python tools/verify_skey.py --show     # 顺便打印每条用例的键
  python tools/verify_skey.py --snapshot # 从内核抽真身，刷新 tools/skey_contract.json

★ 契约快照（为什么需要）：
  cloud 仓的 CI 只 checkout 自己，**拿不到 kernel/app.html** —— 如果只靠"现场抽"，
  这个门禁在云端就等于不存在。所以把内核里那三行实现连同内核指纹一起存成
  `tools/skey_contract.json` 入库；内核一侧改了、没同步刷新快照时，
  本脚本在**本机**立刻报「快照过期」，逼你刷新；云端则用快照守住采集器侧不被改歪。
"""
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
CLOUD = os.path.dirname(HERE)
REPO = os.path.dirname(CLOUD)
KERNEL = os.path.join(REPO, "kernel", "app.html")
SNAPSHOT = os.path.join(HERE, "skey_contract.json")

# 端上那三行的定位锚（改了内核里这三行的写法，这边也要跟着改锚点）
ANCHORS = {
    "norm": "const norm = s=>",
    "cpslice": "const cpslice=",
    "skey": "function skey(t,s){",
}

# 刁难用例：正常中文 / 括号版本号 / 前后空格 / 全角半角混排 /
# 超长标题（触发 40 截断）/ emoji（BMP 外）/ 非 BMP 汉字 / 纯 ASCII / 空值
CASES = [
    ["稻香", "周杰伦"],
    ["孤勇者", "陈奕迅"],
    ["海阔天空 (Live)", "BEYOND"],
    ["《起风了》— 买辣椒也用券", "测试·符号"],
    ["  Spaces  And   Tabs  ", " 歌手 "],
    ["ＡＢＣ全角", "ｄｅｆ半角"],
    ["一首非常非常非常非常非常非常非常非常非常长的歌曲名字用来触发四十字截断测试1234567890", "歌手名字也超级超级超级超级超级超级超级超级长ABCDEFGHIJKLMN"],
    ["Song 🎵 with emoji", "歌手🎤名字"],
    ["𝕊𝕠𝕟𝕘", "𝕊𝕚𝕟𝕘𝕖𝕣"],
    ["𠮷野家の歌", "𠮷𠮷𠮷𠮷𠮷"],
    ["𠮷" * 60, "𠮷" * 50],
    ["ABC-DEF_GHi", "xyz.ko~re"],
    ["【翻唱】演员", "Cover·歌手"],
    ["", ""],
    ["none", None],
]

def extract_from_kernel(path):
    """从内核里抽出真实的三行实现（不复述、不复制，改歪了立刻能被发现）"""
    src = open(path, encoding="utf-8", errors="replace").read()
    out = {}
    for name, anchor in ANCHORS.items():
        i = src.find(anchor)
        if i < 0:
            return None, "内核里找不到锚点 %r（%s）—— 实现改名/换写法了，请同步 ANCHORS" % (anchor, name)
        j = src.find("\n", i)
        out[name] = src[i:j if j >= 0 else len(src)]
    return out, None


def kernel_sha(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def resolve_lines():
    """返回 (三行实现, 来源说明, 需要刷新快照?)"""
    live, err = (None, "kernel/app.html 不存在")
    if os.path.exists(KERNEL):
        live, err = extract_from_kernel(KERNEL)
    snap = None
    if os.path.exists(SNAPSHOT):
        try:
            snap = json.load(open(SNAPSHOT, encoding="utf-8"))
        except Exception:
            snap = None

    if live and snap and snap.get("lines") == live:
        return live, "内核实时抽取（与快照一致）", False
    if live and not snap:
        return live, "内核实时抽取（尚无快照）", True
    if live and snap and snap.get("lines") != live:
        return live, "内核实时抽取（⚠️ 与快照不一致）", True
    if snap and snap.get("lines"):
        return snap["lines"], "契约快照（本 runner 无内核，用 tools/skey_contract.json）", False
    return None, "既没有内核、也没有契约快照：%s" % err, False


def write_snapshot(lines):
    body = {
        "note": "端上 app.html 的 norm/cpslice/skey 真身 + 内核指纹；由 tools/verify_skey.py --snapshot 生成，勿手改",
        "kernel_sha256": kernel_sha(KERNEL) if os.path.exists(KERNEL) else None,
        "lines": lines,
    }
    with open(SNAPSHOT, "w", encoding="utf-8") as fh:
        json.dump(body, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    return SNAPSHOT


JS_DRIVER = r"""
const fs = require("fs");
// ★ 用从内核抽出来的**真身**跑用例（不复述实现）
const L = fs.readFileSync(process.argv[2], "utf8");
const cases = JSON.parse(fs.readFileSync(process.argv[3], "utf8"));
let res;
try {
  res = eval(L + "\n; cases.map(c => skey(c[0], c[1]))");
} catch (e) { console.error("EVAL:" + e.message); process.exit(3); }
console.log(JSON.stringify(res));
"""


def _py_side(cases):
    """采集器侧的键（直接 import localize，保证测的就是生产实现）"""
    sys.path.insert(0, HERE)
    import localize  # noqa
    return [localize._js_skey(t, s) for t, s in cases]


def _node_bin():
    for p in (
        os.environ.get("NODE_BIN"),
        r"C:\Users\sbqqq\.workbuddy\binaries\node\versions\22.22.2-6\node.exe",
        "node",
    ):
        if not p:
            continue
        if p == "node" or os.path.exists(p):
            return p
    return "node"


def main():
    show = "--show" in sys.argv
    lines, origin, need_snap = resolve_lines()
    if not lines:
        print("== skey 契约回归 ==")
        print("  ⚠️  " + origin)
        print("  → 本 runner 无法验证端上侧；请在本机（有 kernel/）跑一次并提交快照：")
        print("     python tools/verify_skey.py --snapshot")
        return 0 if os.environ.get("SKKEY_SOFT") == "1" else 1

    if "--snapshot" in sys.argv or need_snap:
        p = write_snapshot(lines)
        print("已刷新契约快照：%s" % os.path.relpath(p, CLOUD))

    tmp = tempfile.mkdtemp(prefix="skey_")
    js_path = os.path.join(tmp, "drv.js")
    src_path = os.path.join(tmp, "impl.js")
    cs_path = os.path.join(tmp, "cases.json")
    with open(js_path, "w", encoding="utf-8") as fh:
        fh.write(JS_DRIVER)
    with open(src_path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines[k] for k in ("norm", "cpslice", "skey")))
    with open(cs_path, "w", encoding="utf-8") as fh:
        json.dump(CASES, fh, ensure_ascii=False)

    r = subprocess.run([_node_bin(), js_path, src_path, cs_path],
                       capture_output=True, timeout=60)
    if r.returncode != 0:
        print("node 侧执行失败（rc=%d）：%s" % (r.returncode, r.stderr.decode("utf-8", "replace")[:400]))
        return 1
    js = json.loads(r.stdout.decode("utf-8", "replace"))
    py = _py_side(CASES)

    bad = [(i, a, b) for i, (a, b) in enumerate(zip(py, js)) if a != b]
    print("== skey 契约回归（端上 app.html  vs  采集器 localize.py）==")
    print("  实现来源：%s" % origin)
    print("  用例：%d 条（含 emoji / 非 BMP / 超长 / 空值）" % len(CASES))
    if show:
        for i, (a, _b) in enumerate(zip(py, js)):
            print("   #%-2d %r" % (i, a))
    if bad:
        print("  ❌ 有 %d 条不一致 —— 两份实现在某一处已经改歪了：" % len(bad))
        for i, a, b in bad[:10]:
            print("     #%d\n       py=%r\n       js=%r" % (i, a, b))
        print("  → 键对不上 = 图下好了端上查不到（沉淀白做）。必须先修平再发布。")
        return 1
    print("  ✅ 逐字符一致 —— 端上查到的键，采集器都写过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
