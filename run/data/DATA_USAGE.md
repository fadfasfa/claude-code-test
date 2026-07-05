# run/data 数据用途说明

本文是 `run/data` 的用途事实源，用来回答“这个数据为什么存在、谁消费、谁可以写、能不能进包、排障时该看哪里”。目录索引和分类清单分别见 `README.md` 与 `data_manifest.v1.json`。

## 判断口径

新增或移动数据前先按下面顺序判断：

1. 是否是源码态稳定事实源。是则放入 `static/`。
2. 是否只用于首次空仓启动播种。是则放入 `seed/startup/`。
3. 是否是可审计来源输入，但不直接供 UI 读取。是则放入 `evidence/`。
4. 是否是测试或离线诊断样例。是则放入 `fixtures/diagnostics/`。
5. 是否由本机运行、抓取、缓存、调试或用户状态生成。是则放入 `runtime/` 或短期兼容的 `raw/`。

不要用文件名的新旧来决定位置，优先看消费方、写入方和是否可重建。

## static/version

用途：版本级稳定 JSON/TXT 数据，是 Web/API、overlay、清洗脚本和打包白名单共同读取的事实源。

典型文件：

- `英雄目录.v1.json`
- `海克斯资源目录.v1.json`
- `Champion_Synergy_Cleaned.json`
- `hero_version.txt`

主要消费方：

- Web API 的 `/data/static/...` 与 `/data/indexes/...` 兼容路由。
- overlay hint cache 和视觉识别模板索引。
- `hextech.catalog.version_catalog` 的旧文件名投影。
- `tools.bundle_manifest` 与 `tools.package_rules` 的打包 manifest。

写入规则：

- 只能由明确的数据同步、清洗或资源刷新工具写入。
- 写入 cleaned 协同数据时必须保留 ApexLoL 优先、ARAMMayhem 只补缺的语义。
- 不写入抓取中间态、运行缓存、调试转储、锁或用户状态。

打包规则：允许随包分发。

排障提示：如果 Web 或 overlay 显示旧数据，先看这里的 cleaned/static 文件，再看 `runtime/state` 里的刷新状态，不要先看 raw。

## static/assets

用途：随包分发的图片、图标和视觉识别需要的稳定资源。

典型文件：

- 海克斯图标。
- 英雄、装备或识别模板相关 PNG。

主要消费方：

- Web `/assets/...` URL。
- overlay 视觉识别和图标解析。
- 打包白名单。

写入规则：

- 由 CDragon/图标同步工具和 overlay 识别资源刷新工具写入。
- 发布新资源时只追加或替换明确文件，不把运行态 debug 图片混入。

打包规则：允许随包分发，但只允许白名单内扩展名和文件形态。

排障提示：图标 404 或识别模板缺失时看这里；真机临时截图应写入 `runtime/debug/`，不应放进这里。

## seed/startup

用途：便携包首次空仓启动时使用的冷启动 seed。它是构建期输入，不是 UI 的长期事实源。

典型文件：

- `hextech/Hextech_Data_*.csv`
- `synergy/Champion_Synergy_YYYYMMDD_HHMMSS.json`
- `synergy/Champion_Synergy_latest.v1.json`

主要消费方：

- `tools.bundle_manifest`
- `tools.runtime_bundle`
- 打包启动烟测

写入规则：

- 只在准备更新首启基线时写入。
- 不因普通运行刷新自动覆盖。
- 不用它判断当前线上新鲜度；运行后状态以 `runtime/state` 和 active CSV/latest 为准。

打包规则：允许随包分发，包内路径必须保持 `data/seed/startup/...`，其中联动 seed 固定放在 `data/seed/startup/synergy/`。

排障提示：空仓包首次启动没数据时看这里；已运行过的本机显示旧数据时优先看 `runtime/raw` 和 `runtime/state`。

## evidence

用途：保留可审计的外部来源输入，用于复现清洗结果。它不是 Web/API 或 overlay 的真相源。

典型文件：

- `mayhem_combos.raw.json`

主要消费方：

- Mayhem 清洗/合并脚本。
- 数据审计和回放。

写入规则：

- 更新 evidence 后必须重新生成对应的 `static/version` cleaned 数据。
- 不允许前端、API 或 overlay 直接读取 evidence 作为展示数据。

打包规则：默认不进普通运行包。

排障提示：如果 ARAMMayhem 补缺数量异常，先看 evidence raw 是否更新，再看 cleaned merge summary。

## fixtures/diagnostics

用途：离线诊断、视觉匹配和回归测试样例。

典型文件：

- `overlay_matching_truth.v1.json`
- `overlay_vision_fixtures/**`

主要消费方：

- overlay 视觉评测工具。
- `dev_checks` 与 pytest 回归。

写入规则：

- 只放可复现、可审计的测试样例。
- 真机临时转储、失败截图和调试 dump 写入 `runtime/debug/`。

打包规则：默认不进普通运行包，除非以后显式创建测试包。

排障提示：离线视觉回归失败时看这里；真实游戏运行失败时看 `runtime/debug` 和 state。

## runtime

用途：本机运行态根。所有启动后生成、缓存、状态、日志、锁、profile、debug 和 persisted 数据都应进入这里。

子目录：

- `raw/`：运行态抓取原始产物。
- `state/`：启动状态、刷新状态、端口文件和功能开关。
- `cache/`：可重建缓存和预计算结果。
- `logs/`：运行日志。
- `reports/`：抓取报告和诊断报告。
- `debug/`：临时调试转储。
- `profile/`：本地 profile 状态，不含真实浏览器凭据。
- `locks/`：运行锁。
- `persisted/`：需要跨次运行保留但不进包的本机状态。

主要消费方：

- 桌面 UI、Web 服务、后台刷新、诊断工具和自愈 worker。

写入规则：

- 运行态工具可以写入，但不能反向污染 `static/`、`seed/`、`evidence/` 或 `fixtures/`。
- 失败状态也必须结构化写回，不能只留日志。
- 不读取或写入 token、cookie、真实浏览器 profile 或凭据内容。

打包规则：禁止进入普通包。

排障提示：刷新失败、fallback、active CSV、in_progress 泄漏和抓取 backend 都应能在 `runtime/state` 或 `runtime/reports` 中定位。

## legacy raw

源码态历史上存在 `run/data/raw/**`。本次强迁移后该路径不再作为真实读取入口，也不保留兼容目录；如果本机仍残留空目录或旧文件，应按迁移清单确认后移入 `data/runtime/raw/**` 或丢弃可重建缓存。

打包规则：禁止进入普通包。
