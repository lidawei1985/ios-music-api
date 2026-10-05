# ios-music-api — 大伟哥 MUSIC 自有公网音源池

**架构铁律**：第三方只是原料，必须经本引擎**体检 → 打分 → 归一 → 择优**后以**自有 pool.json** 发布。
App 端**只认自家 pool.json**，按分数降序试源，首个能播即播 —— 不硬依赖任何一家第三方解析服务。

## 为什么这样做（不依赖别人的东西）
- 平台公开接口（QQ/酷狗/网易云/B站…）是平台自己开在公网上的**公共基础设施**，比某个第三方解析 API 稳得多；
  适配器是**我们自己的代码**，不是别人的服务。
- 任何单一源挂掉 → 分数下滑 → 自动沉底；我们自动从候选池里试出可用的顶上来。**能力属于我们自己的仓库。**
- 全流程跑在 **GitHub Actions（公网 / 定时 / 与本机是否开机无关）**，产出经 **jsDelivr CDN** 免费分发。

## 产出契约（App 端消费）
- `GET data/pool.json`
  ```json
  {
    "updated": "2026-10-05T06:00:00Z",
    "round": 42,
    "sources": [
      {"id":"qq","kind":"platform","name":"QQ音乐","status":"active",
       "score":94.2,"metrics":{"chain_ok":0.93,"latency_ms":420,"quality":"320k","stable_rounds":12},
       "history":[[1700000000,94.2], ...]}
    ],
    "quarantined": ["..."],
    "probes": [{"title":"稻香","singer":"周杰伦"}]
  }
  ```

## 守护线（无人值守安全）
- 单轮墙钟预算，绝不跑爆 Actions 上限
- 连续 N 轮取链失败 → `quarantined`（隔离但保留，可自动恢复）
- 候选源每轮小批量试跑，连续合格才升 `active`（**自动补给**）
- 每轮结果写 history，供"稳定性"评分与外部审计

## 分发地址
- 池：`https://cdn.jsdelivr.net/gh/lidawei1985/ios-music-api@main/data/pool.json`
- jsDelivr 有缓存，工作流内会调 purge 主动刷新。
