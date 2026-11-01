# -*- coding: utf-8 -*-
r"""★ git merge driver：data/catalog/shard-*.json 冲突时按「歌曲 id 取并集」自动合并。

============================= 为什么需要这个 =============================
harvest.yml 与 pool.yml 各自有独立的 concurrency group（2026-10-08 刻意拆开：
共用一组时 harvest 被 pool 反复顶成 cancelled，曲库冻结过 23 小时）。
代价是两条流水线**会真的并发跑**，而它们都要往 data/catalog/shard-*.json 里写
（新增歌曲、改 src/热度字段）。于是发布步骤的 `git rebase --autostash origin/main`
在 shard 上撞出行级冲突 → 脚本 `rebase 冲突，放弃本次发布` → 整轮 1~2 小时的
采集成果随 runner 一起销毁。2026-11-01 13:23Z 那轮就是这样失败的。

shard 的语义是「歌曲集合」，两边都是往里加东西 —— 天然可以按 id 取并集，
不需要人工裁决。本脚本就是这个裁决器。

用法（git 三向合并协议）：
    git config merge.dwgid.name "DWG shard union by song id"
    git config merge.dwgid.driver "python tools/merge_shard.py %O %A %B"
    # %O = 共同祖先版本   %A = 我们这边的（结果必须写回这里）   %B = 对方版本
退出码 0 = 合并成功；非 0 = 交回 git 报冲突。

用法（命令行自检）：
    python tools/merge_shard.py <base> <ours> <theirs> [out]
"""
import json
import os
import sys


def _load(p):
    """读一个 shard JSON；读不出来就当空列表（不要因此让整个合并崩掉）"""
    try:
        with open(p, encoding="utf-8") as f:
            d = json.load(f)
    except Exception:
        return []
    return d if isinstance(d, list) else []


def _key(x):
    """歌曲唯一键：优先 i(id)，退回 归一化歌名|歌手 —— 防止同曲被判成两首留重复"""
    if isinstance(x, dict):
        i = x.get("i")
        if i not in (None, "", 0):
            return "i:%s" % i
        return "n:%s|%s" % (str(x.get("n") or "").strip(),
                            str(x.get("a") or "").strip())
    return "raw:%s" % json.dumps(x, ensure_ascii=False, sort_keys=True)


def _richness(x):
    """字段越全越可信：合并同一首歌时挑信息量大的那条，别让空壳覆盖有热度的"""
    if not isinstance(x, dict):
        return 0
    return sum(1 for k, v in x.items()
               if v not in (None, "", 0, [], {}) and k != "i")


def merge(base, ours, theirs):
    """并集 + 同键择优；保持 ours 的顺序（先来的在前），对方新增的追加在后"""
    out, idx = [], {}
    for x in ours:
        k = _key(x)
        idx[k] = len(out)
        out.append(x)
    for x in theirs:
        k = _key(x)
        if k not in idx:
            idx[k] = len(out)
            out.append(x)
        else:
            j = idx[k]
            if _richness(x) > _richness(out[j]):
                out[j] = x
    return out


def main():
    if len(sys.argv) < 4:
        print(__doc__)
        return 2
    o, a, b = sys.argv[1], sys.argv[2], sys.argv[3]
    outp = sys.argv[4] if len(sys.argv) > 4 else a

    base, ours, theirs = _load(o), _load(a), _load(b)
    res = merge(base, ours, theirs)
    with open(outp, "w", encoding="utf-8") as f:
        json.dump(res, f, ensure_ascii=False, indent=1)
    if len(sys.argv) > 4:
        print("base=%d ours=%d theirs=%d → %d"
              % (len(base), len(ours), len(theirs), len(res)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
