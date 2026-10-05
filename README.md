# ios-music-api — 大伟哥 MUSIC 自有公网音源池

**架构铁律**：第三方只是原料，必须经本引擎**体检 → 打分 → 归一 → 择优**后以**自有 pool.json** 发布。
App 端**只认自家 pool.json**，按 `order` 依次试源，首个能播即播 —— 不硬依赖任何一家第三方解析服务。

## 为什么这样做（把别人的变成自己的）

- 平台公开接口是我们**自己重写实现**的（含自研签名：QQ `zzb`、酷狗 `salt-md5`），代码在本仓；
  平台改字段只改本仓一行，App 完全无感。
- 数据自持：抓下来的曲目/歌手/封面沉淀成 `data/library.json` + `data/artists.json`，**越跑越全**。
- 地址自持：产出只走本仓 + jsDelivr，App 里**不出现任何第三方域名**。
- 全流程跑在 **GitHub Actions（公网 / 定时 / 与本机是否开机无关）**。

## 产出契约（App 端消费）

| 文件 | 用途 |
|---|---|
| `data/pool.json` | 源池状态 + `order`（App 试源顺序）+ 每源分数/分档/历史 |
| `data/charts.json` | 我们规范化后的榜单（8 个榜）+ 热搜词 |
| `data/library.json` | **自有歌库**：曲目元数据 + 各源 id 映射（本地搜索秒出） |
| `data/artists.json` | **歌手头像库**：歌手名 → 我们自己的头像地址 |

`pool.json` 关键字段：

```json
{
  "updated": "2026-10-05T06:40:00Z",
  "round": 3,
  "order": ["wy", "migu", "bili"],
  "hifi": ["wy", "migu"],
  "backstop": ["bili"],
  "sources": [
    {"id":"migu","name":"咪咕音乐","role":"hifi","status":"active","score":84.0,
     "chain_ok":0.8,"latency_ms":345,"quality_score":0.93,"history":[[1700000000,84.0]]}
  ],
  "replenish_log": [{"source":"bili","action":"auto-promoted","score":90.1}]
}
```

## 分层试源为什么重要（实测结论）

| 源 | 角色 | 实测 | 说明 |
|---|---|---|---|
| 咪咕 | hifi | 16/20 | **匿名仅免费曲**；VIP 曲回 `200002`（错误信息是误导性的"参数格式错误"）。免费曲常给 **SQ 无损** |
| 网易云 | hifi | 13/20 | `outer/url` 匿名可用；VIP/版权曲无跳转 |
| B站 | backstop | **20/20** | 全覆盖兜底（视频音轨），但属二创/转码，故排在高保真源之后 |
| QQ | auth | 0/20 | 签名正确，但取链一律 `result=104003` → 必须登录 |
| 酷狗 | auth | 0/20 | 签名正确，播放口 `err_code=30020` → 必须 token |

> **没有任何单源能覆盖全部**。真正的 100% 来自 `order` 里的**跨源依次兜底**。
> 这正是"把第三方变成我们自己的"的意义：单源挂了，我们不掉能力，只是顺序变了。

## 守护线（无人值守安全）

- `tools/validate.py` 硬校验：**没有可用源 / 歌库过小 / 榜单全空 → 拒绝发布**，绝不静默产出空池。
- 单轮墙钟预算，跑不爆 Actions 上限（实测 20 探针 ≈ 33s）。
- 连续失败 → `needs_auth` / `quarantined`（隔离但保留，可自动恢复）。
- 每轮写 history，供"稳定性"评分与外部审计。

## 分发地址

- 池：`https://cdn.jsdelivr.net/gh/lidawei1985/ios-music-api@main/data/pool.json`
- 歌库：`.../data/library.json`　榜单：`.../data/charts.json`　头像：`.../data/artists.json`
- jsDelivr 有缓存，工作流内会调 purge 主动刷新。

## 本地复现

```bash
python tools/adapters.py 稻香 周杰伦   # 自研适配器自检（逐源 搜索→取链→真字节校验）
PROBES_LIMIT=6 python tools/build.py   # 跑一轮维护（本地快测）
python tools/validate.py               # 校验产出
```
