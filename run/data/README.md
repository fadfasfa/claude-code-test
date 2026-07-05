# Hextech 数据目录

`run/data` 是源码态唯一数据根。`run/resources` 已移除，新的代码、工具、测试和打包规则都不得再把 `resources/` 当作真实文件来源。

本目录同时承载稳定随包数据、首启 seed、可审计来源证据、离线测试样例和本机运行态数据。判断一个文件该放哪里时，先看“谁消费、是否随包、是否可重建、是否包含本机状态”。具体用途、写入方、消费方和排障口径见 [DATA_USAGE.md](DATA_USAGE.md)。

## 目录用途

| 路径 | 用途 | 主要消费者 | 打包策略 | 写入规则 |
| :--- | :--- | :--- | :--- | :--- |
| `static/version/` | 版本级稳定 JSON/TXT。包含英雄目录、海克斯资源目录、cleaned 联动数据和版本号。 | Web、overlay、桌面 UI、清洗脚本、打包 manifest | 进入包内 `data/static/version/` | 只写稳定事实源；不得放 runtime、日志、profile、抓取中间产物 |
| `static/assets/` | 稳定图片和图标。Web 对外仍可暴露 `/assets/...` 兼容 URL。 | Web 静态资源、overlay 视觉模板、图标解析器 | 进入包内 `data/static/assets/` | 只放图片和维护 README；非图片文件会被打包检查拒绝 |
| `seed/startup/` | 首启种子快照。用于打包后首次启动播种运行态数据。 | `tools/bundle_manifest.py`、`tools/runtime_bundle.py`、打包烟测 | 进入包内 `data/seed/startup/...` | 只放可作为冷启动 seed 的快照；不是 UI 真相源 |
| `evidence/` | 来源证据。用于复现 cleaned 数据，例如 ARAMMayhem raw。 | 数据清洗脚本、审计和回放 | 默认不直接作为 UI 数据；可作为构建输入 | 不直接供 Web/API 读取；更新后必须重新生成对应 cleaned/static 数据 |
| `fixtures/diagnostics/` | 离线诊断样例和真值。 | overlay 评测、诊断工具、测试 | 不进普通运行包，除非显式测试包需要 | 只放可复现测试样例；真机临时转储写 `runtime/debug/` |
| `runtime/` | 本机可变运行态，含 `raw/state/cache/logs/reports/debug/profile/locks/persisted`。 | 运行中服务、抓取器、诊断采集 | 禁止进入包 | 可写；默认 ignored；不得作为源码态稳定数据事实源 |

## 消费边界

- Web 和 overlay 的联动展示读取 `static/version/Champion_Synergy_Cleaned.json`。raw、evidence、cache、report 都不是 UI 真相源。
- 高频 Hextech 战报 CSV 运行时写入 `runtime/raw/hextech/`。有效 CSV 可作为当前 active 数据，但不随包直接发布。
- ApexLoL/ARAMMayhem 原始联动抓取结果先进入运行态或 evidence，再由清洗合并脚本生成 `static/version/Champion_Synergy_Cleaned.json`。
- 打包只允许稳定源码、`static/version`、`static/assets` 和明确的 `seed/startup` 进入包；`runtime/**`、旧 `raw/**`、旧 `processed/**`、`__pycache__`、`.pyc` 一律禁止。
- 历史 `data/raw/**` 不再作为真实读取入口；发现残留时按迁移清单移入 `runtime/raw/**` 或丢弃可重建缓存。

## 维护规则

- 新增数据前先选择目录分类，并同步 `data_manifest.v1.json`、打包白名单和相关测试。
- 不读取或提交凭据、cookie、token、真实浏览器 profile、账号池、proxy 配置或本机登录态。
- 不把临时诊断输出、日志、缓存、锁、profile、debug 截图放入 `static/`、`seed/` 或 `evidence/`。
- `static/version` 中的 cleaned 数据可以由脚本生成，但生成前必须保留旧有效数据的熔断语义，避免空抓取覆盖可用数据。
