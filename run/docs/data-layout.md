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
fallback，`seeds` 只用于空仓首启。在线 Catalog 与图片分别写入
`var/catalog/generations` 和 `var/cache/assets`，不得回写 bundle。

## 本机运行态

```text
var/
├── catalog/{current.v2.json,generations/<catalog_generation_id>/}
├── sources/
│   ├── hextech/{current.v2.json,runs/<run_id>/{stats.csv,manifest.json,report.json}}
│   ├── apex/{current.v2.json,runs/<run_id>/{synergy.json,manifest.json,report.json}}
│   └── mayhem/{current.v2.json,runs/<run_id>/{combos.json,manifest.json,report.json}}
├── snapshots/{current.v2.json,previous.v2.json,generations/,staging/}
├── state/                     # 跨进程契约可平铺，服务私有状态使用子目录
│   └── data-service/{candidates/,refresh_schedule.v1.json,promotion_journal.v1.json}
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

- Catalog 和三个来源先写 immutable generation/run 与 candidate pointer，抓取器不直接替换正式 current。
- `var/catalog/current.v2.json` 与 `var/sources/<source>/current.v2.json` 必须绑定同一 Catalog cohort。
- 失败 run 可保留 `manifest.json` 和 `report.json`，但不得切 current。
- Mayhem 只补 Apex 缺失组合，不覆盖相同英雄和组合。
- DataService 通过 `state/data-service/promotion_journal.v1.json` 统一提升 cohort：先切依赖 pointer，最后切 `var/snapshots/current.v2.json`；异常后可整体回滚或向前完成。
- journal 未提交时，strict verifier 通过 cohort resolver 继续读取旧 pointer，不直接观察 promotion 中间态。
- Desktop 和 Web 在一次请求内固定 `DataSnapshotView`；Overlay 在同一 `session_id + selection_epoch` 内固定同一个 view，下一轮才允许换代。

排障顺序：先看 `var/sources/*/runs/<run_id>/report.json`，再看来源 current，最后看 snapshot current 和 generation manifest。不要从单个 CSV/JSON 文件推断当前线上代。
