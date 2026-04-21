# Wiki 数据源层

## 功能介绍（头部）

该目录是 `sm2-randomizer` 的 Wiki 数据采集入口，负责从公开页面抓取职业、武器、天赋相关源数据，并沉淀为后续计算层可消费的中间产物。

核心能力：
- 统一 Python 入口调度结构抓取与资源抓取（`run.py`）
- 抓取并生成职业/武器原始数据与审阅报表
- 抽取天赋树布局坐标（`col/row`）并对齐到清单层
- 仅输出 source/catalog/reports 层数据，不直接写运行时数据

快速开始：
- 运行完整 Wiki 采集：`python run.py`
- 跳过结构抓取：`python run.py --skip-structure`
- 跳过资源刷新：`python run.py --skip-assets`

该目录只负责 Wiki 抓取，不直接服务前端运行。

当前维护口径：

- Python 是长期目标运行时，`scrape_wiki.py` 是推荐入口。
- 现有 `scrape_wiki.js` 仍保留为过渡实现，后续会逐步迁入 Python。
- `node_modules/` 不属于仓库正式内容，需要时按 `package.json` / `package-lock.json` 重建。

## 文件

- `scrape_wiki.py`
  - Python 侧推荐入口
  - 当前负责调度现有 Wiki 结构抓取实现，并为后续单运行时迁移提供稳定命令口径
- `scrape_wiki.js`
  - 过渡期的 Wiki 结构抓取实现
  - 抓取职业、武器、官方候选资源、审阅种子
  - 输出到 `pipeline/out/source/wiki/` 与 `pipeline/out/source/reports/`
- `scrape_perks.py`
  - 抓取职业图与天赋图标，下载到 `app/assets/`
  - 同步更新 `pipeline/out/source/wiki/原始抓取数据.json` 与 `pipeline/out/source/catalog/`
- `source_anchor.json`
  - 仅保留一个 Wiki 锚点 URL，供未来重建抓取链使用

## 约束

- 抓取脚本不能直接写 `app/data/`
- 抓取脚本只产生源数据和源报告
- 进入运行层前必须经过 `pipeline/compute/*`
- 天赋坐标以 Wiki 页面真实 `col/row` 为准，不再使用 `index` 反推布局
- `pipeline/out/source/catalog/` 是当前流程依赖的清单层，不再使用 `legacy/` 命名
