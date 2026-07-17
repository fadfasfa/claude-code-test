# 数据布局与维护路径

## 可提交资源

```text
resources/
├── catalog/
│   ├── 英雄目录.v1.json
│   ├── 海克斯资源目录.v1.json
│   └── hero_version.txt
├── assets/
├── seeds/
│   ├── current.v1.json
│   └── generations/<generation_id>/
│       ├── champions.json
│       ├── champion_hextech.json
│       ├── overlay_hints.json
│       ├── identities.json
│       └── manifest.json
└── evidence/mayhem/
```

`resources/catalog` 是稳定身份目录，不能被一次抓取失败覆盖。`resources/seeds` 只用于空仓首启，必须是一代可通过 manifest、数量和 SHA-256 校验的完整 generation。

## 本机运行态

```text
var/
├── sources/
│   ├── hextech/{current.v1.json,runs/<run_id>/{stats.csv,manifest.json,report.json}}
│   ├── apex/{current.v1.json,runs/<run_id>/{synergy.json,manifest.json,report.json}}
│   └── mayhem/{current.v1.json,runs/<run_id>/{combos.json,manifest.json,report.json}}
├── snapshots/{current.v1.json,previous.v1.json,generations/,staging/}
├── state/{desktop,data-service,supervisor,overlay,web}/
├── ipc/
├── cache/{vision,scraping}/
├── profiles/
├── logs/
├── reports/
└── locks/
```

冻结包固定写 `%LOCALAPPDATA%/HextechNexus/var`。源码态默认写 `run/var`，测试可通过 `HEXTECH_VAR_DIR` 指向隔离目录。

## 指针与写权限

- `var/sources/<source>/current.v1.json` 只指向通过该来源完整性门禁的 run。
- 失败 run 可保留 `manifest.json` 和 `report.json`，但不得切 current。
- Hextech、Apex、Mayhem 互不改写对方 current。
- Mayhem 只补 Apex 缺失组合，不覆盖相同英雄和组合。
- DataService 固定读取每个 source current，构建候选 generation，并在全部文件校验后原子切换 `var/snapshots/current.v1.json`。
- Desktop、Web、Overlay 在一次请求或会话内固定同一个 `DataSnapshotView`，不得跨代混读。

排障顺序：先看 `var/sources/*/runs/<run_id>/report.json`，再看来源 current，最后看 snapshot current 和 generation manifest。不要从单个 CSV/JSON 文件推断当前线上代。
