# 数据布局与维护路径

## 可提交资源

```text
resources/
├── manifest.v2.json
├── catalog/
│   ├── manifest.v2.json
│   ├── 英雄目录.v1.json
│   ├── 海克斯资源目录.v1.json
│   └── hero_version.txt
├── assets/
├── seeds/
│   ├── current.v2.json
│   └── generations/<generation_id>/
│       ├── champions.json
│       ├── champion_hextech.json
│       ├── overlay_hints.json
│       ├── identities.json
│       └── manifest.json
└── evidence/mayhem_combos.raw.json
```

`resources/**` 在运行时严格只读：`catalog` 是稳定 seed，`assets` 是包内
fallback，`seeds` 提供已验证启动基线，不能覆盖更新的本地完整代。在线 Catalog 与图片分别写入
`var/catalog/generations` 和 `var/cache/assets`，不得回写 bundle。

## 本机运行态

```text
var/
├── catalog/{current.v2.json,generations/<catalog_generation_id>/}
├── sources/
│   ├── hextech/{current.v2.json,runs/<run_id>/{stats.csv,manifest.json,report.json}}
│   ├── aramkit/{current.v2.json,runs/<unit_run_id>/{manifest.json,rankings.json|scoped_stats/}}
│   ├── blitz/{current.v2.json,runs/<run_id>/}
│   ├── apex/{current.v2.json,runs/<run_id>/{synergy.json,manifest.json,report.json}}
│   └── mayhem/{current.v2.json,runs/<run_id>/{combos.json,manifest.json,report.json}}
├── snapshots/{current.v2.json,previous.v2.json,generations/,staging/}
├── raw-responses/<source_sha256>/<revision_sha256>/{manifest.json,*.body}
├── recognition/{selection-cache-v2/,failure-inbox/}
├── state/                     # 跨进程契约可平铺，服务私有状态使用子目录
│   └── data-service/{candidates/,refresh_schedule.v1.json,promotion_journal.v1.json,cohort_recovery_point.v1.json,download_context.v1.json}
├── user-data/preferences/
├── cache/{overlay_vision,assets}/
├── profiles/
├── logs/
├── reports/
└── locks/
```

冻结包固定写 `%LOCALAPPDATA%/HextechNexus/var`。源码态默认写 `run/var`，测试可通过 `HEXTECH_VAR_DIR` 指向隔离目录。

仓库根 `.archive/hextech-data-v1-*/` 只保存人工归档的旧 `run/data`，不属于
运行态、资源 fallback 或 retention 扫描范围。旧浏览器 profile 在归档中保持
不透明，不读取内容、不列文件名、不计算摘要。

## 指针与写权限

- Catalog 和来源先写 immutable generation/run 与 candidate pointer，抓取器不直接替换正式 current。`hextech/stats` 为历史兼容；当前生产核心是 ARAMKit 排行与独立英雄 unit，Optional 为 Blitz/Apex/Mayhem。
- 统计 snapshot 与其来源 unit 必须保持同一已验证 Catalog 绑定；独立识别 `var/catalog/current.v2.json` 可以是另一代已完整验证的较新 Catalog，不要求统计重抓后才发布，不重写旧 unit 的 Catalog ID。
- 失败 run 可保留 `manifest.json` 和 `report.json`，但不得切 current。
- Mayhem 只补 Apex 缺失组合，不覆盖相同英雄和组合。
- DataService 通过 `state/data-service/promotion_journal.v1.json` 统一提升 cohort：先切依赖 pointer，最后切 `var/snapshots/current.v2.json`；异常后可整体回滚或向前完成。
- journal 未提交时，strict verifier 通过 cohort resolver 继续读取旧 pointer，不直接观察 promotion 中间态。
- Desktop 和 Web 在一次请求内固定 `DataSnapshotView`；Overlay 在首次选择开始前仅可采用当前英雄已完整的新 view，首次选择截止后整局固定，下一 epoch 仅更新阶段，下一局重新开放采用。

新识别 Catalog 增加 `augment_identities.v2.json`（metadata 身份与启用/能力）并继续保留 `augment_assets.v1.json`（视觉资产）；缺图标不等于删身份，缺统计不等于不可识别。Supervisor 消费 DataService 已发布的独立 Catalog，局中不切矩阵。启动恢复和 retention 同时验证/保护独立 active Catalog 与旧统计闭包，receipt 不能证明时回退完整验证。

`recognition/selection-cache-v2` 是默认有界选择区域缓存（200 组/256 MiB），manifest 包含 `slot_rois`；人工、已标记、未知和部分资产受保护且计入容量。`failure-inbox` 为独立有限失败记录。两者都不自动生成 truth 或晋升 exemplar，也不清理历史 case/truth/debug 目录。具体写入、保护和验收边界见 [overlay-adaptive-recognition.md](overlay-adaptive-recognition.md)。

## V3 单元闭包与原始响应缓存

Snapshot manifest 新写入 schema 3、读取兼容2/3，current/previous文件名与pointer schema仍为v2。`components.ranking` 和 `components.champions[hero_id]` 记录 source_version/catalog_id/complete/run_id 等身份；一个 snapshot 可引用多个 ARAMKit run，不能用单个 source current 代替整个闭包。rank-only 时英雄显式 pending，缺 Optional 不伪造来源文件。恢复点文件名保持 `cohort_recovery_point.v1.json`，其 schema2 绑定 snapshot3及完整units；旧schema1仍用于v2。保留集合必须覆盖current/previous/recovery/journal引用的全部run、Catalog与child。

生产 worker 的 RawResponseCache 根为 `var/raw-responses`。source和revision目录使用SHA-256，body名来自URL/内容身份；manifest是提交标记，不把URL明文写作路径。默认每revision 2GiB、每来源6GiB，未完成/孤立body仍计预算；命中需回读hash/size，同revision可续传复用，跨revision不复用。容量耗尽明确停止写入，当前没有自动prune或“两版本自动淘汰”；它不属于diagnostic_retention，不能扫描其他目录凑空间。调用方可显式注入测试缓存根，但正式资源仍只读。

排障顺序：先看 `var/sources/*/runs/<run_id>/report.json`，再看来源 current，最后看 snapshot current 和 generation manifest。不要从单个 CSV/JSON 文件推断当前线上代。
