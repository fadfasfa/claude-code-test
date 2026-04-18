# 项目文档 — sm2-randomizer

## 当前结构

项目当前采用“运行层 + 离线工坊 + 调试入口”的稳定结构：

- `app/`
  - 最终运行层
  - 包含 `static/` 前端入口、运行期冻结数据、运行所需本地资源
  - 最终打包 `exe` 时只应包含这一层
- `debug/`
  - 本地静态调试入口
  - 只负责把工作区根目录暴露为静态站点，并默认进入 `app/static/`
- `pipeline/`
  - 离线数据工坊
  - 负责 Wiki 抓取、Excel 导入、人工补录、多源归并、运行数据生成、校验

## 数据模型

当前不是 Excel 单真源，而是“Wiki 主导、Excel 辅助勘误”的混合模型：

- `Wiki`
  - 职业/武器/天赋结构
  - 可用性与版本新增项
  - 职业图候选、天赋图候选、描述主干
  - 职业随机武器池唯一来源
  - 未来的首选主源
- `Excel`
  - 武器图片
  - 武器中文名基线
  - 表格型整理字段与补录勘误
  - 仅作为补充与修正源，不再视作长期主源
- `人工补录`
  - 技能条目
  - 策略词条
  - 负面词条抽取规则、互斥组合与配额限制
  - 关键例外修正

武器名称与图片命名当前遵循：

- 优先使用 `Excel` 中文名
- 若 Excel 缺失，则回落到现有中文名
- 少量例外仅通过 `pipeline/sources/manual/weapon_image_name_overrides.json` 显式覆盖
- 武器图片目录当前以 Excel 的工作表归档为准；不再额外按推断槽位重分目录
- Excel 武器块提取默认按标题栏紫色背景排除英雄武器；`双联热熔枪` 当前按英雄武器处理，不进入正式运行数据
- 对于 Excel 已确认但旧 Wiki / 旧展示映射暂未覆盖的新主武器，允许在归并层做最小职业池覆盖；当前包括 `神射手卡宾枪 -> 狙击兵`

武器图片目录固定为：

- `app/assets/weapons/icons/主武器/`
- `app/assets/weapons/icons/副手/`
- `app/assets/weapons/icons/近战/`

当前模式固定为：`hybrid_now_wiki_ready`

含义：

- 当前以多源汇总为准
- 结构上预留未来切换到 Wiki 主源
- 前端不直接读取抓取或导入中间文件

## 关键目录

- `app/static/index.html`
  - 纯静态前端入口
- `app/static/main.js`
  - 本地随机抽取逻辑
- `app/static/styles.css`
  - 本地样式
- `app/data/classes.json`
  - 职业运行数据
  - 每个职业保留 `slug/name/role/tagline/summary/class_ability/image_path`
  - `loadout_pools` 仅保留主/副/近战三类武器池
  - 武器项统一为 `slug/name/slot_type/image_path`（不再输出 `original_name` 与 `all` 池）
- `app/data/talents.json`
  - 天赋矩阵运行数据
  - 每个职业保留 `grid_spec` 与完整可渲染的 `nodes`
  - `description` 运行期默认输出中文展示文案；当前优先使用手工中文描述映射，其次回落到原始描述
- `app/data/meta.json`
  - 运行期构建信息、正面词条池、负面词条池、负面词条规则
  - 负面词条 `label` 会按前端宽度限制进行统一缩略，并保持 `detail` 完整
- `serve_static.py`
  - 本地调试入口；根路径会默认进入 `app/static/`
- `pipeline/collect/wiki/`
  - Wiki 抓取脚本与锚点 URL（聚焦职业图/天赋图标与天赋描述）
  - `run.py` 是当前推荐入口；Python 为长期目标运行时
  - 职业页入口固定为 `https://spacemarine2.fandom.com/wiki/<ClassName>`
- `pipeline/collect/wiki/scrape_perks.py`
  - 负责复用/抓取职业图、天赋图标并刷新 catalog manifest
  - 构建时会自动清理未被 manifest 引用的占位 SVG
- `pipeline/collect/excel/`
  - Excel 文件与 Excel 导入脚本（武器图片与词条清单）
- `pipeline/collect/manual/`
  - 人工补录数据
  - 当前也承载负面策略词条抽取规则的结构化定义
  - `talent_description_zh.json` 维护天赋中文描述映射，按职业分组以避免重复 slug 冲突
- `pipeline/collect/rules/field_source_policy.json`
  - 字段来源策略
- `pipeline/collect/rules/extraction_rules.json`
  - 抽取与契约规则
- `pipeline/compute/merge_sources.py`
  - 多源归并
- `pipeline/compute/build_runtime_data.py`
  - 生成最小运行期 3 份 JSON，可输出到候选目录并清理旧运行期文件
- `pipeline/compute/validate_runtime_data.py`
  - 校验运行数据
- `pipeline/compute/publish_candidate.py`
  - 生成候选 diff、应用候选运行数据并清理候选目录
- `pipeline/store/raw/`
  - 离线原始抓取与导入产物
  - 默认视为可重建工坊产物；仅长期维护入口型文件保留入库
- `pipeline/store/catalog/`
  - 当前有效的清单层目录
- `pipeline/store/reports/`
  - 校验报告与抓取/发布过程报告
- `pipeline/tmp_publish/`
  - 候选运行数据输出目录，用于发布前校验与 diff

## 运行与构建

推荐顺序：

```powershell
python -m pipeline.collect.wiki.run --headless
python -m pipeline.collect.excel.run
python build_release.py build-candidate
python build_release.py diff-candidate
python build_release.py apply-candidate
python serve_static.py
```

补充说明：

- `build_release.py refresh-data` 会串起 Wiki/Excel 刷新、候选运行数据构建、校验与 diff 生成
- `build_runtime_data.py` 当前会强制执行一次最新 `merge_sources()`，默认写入 `pipeline/tmp_publish/`
- `apply-candidate` 只会在候选运行数据校验通过后把 `classes.json`、`talents.json`、`meta.json` 应用到 `app/data/`
- `build_runtime_data.py` 会显式删除旧的 `loadouts.json`、`talent_details.json`
- 不应再把旧的 `pipeline/out/runtime/merged_data.json` 缓存视作前端数据是否已更新的唯一依据

## 约束

- `app/` 不允许依赖 `pipeline/` 里的 Python 或 Node 逻辑
- 前端只允许读取 `app/data/classes.json`、`talents.json`、`meta.json`
- Wiki 抓取脚本必须保留，但只能写入 `pipeline/out/source/`
- 职业可用武器随机池完全以 Wiki 为准，`front_display` 或旧展示稿不再作为该字段来源
- Excel 导入脚本必须保留，但角色限定为图片补充、勘误与中文名辅助
- 大体量候选 URL、DOM dump、截图、验证中间文件不得进入 `app/data/`
- `pipeline/out/source/catalog/` 是当前有效的清单层命名；`legacy/` 不再作为稳定接口
- `pipeline/sources/wiki/node_modules/`、`__pycache__/` 与一次性调试证据不属于正式仓库内容
- `app/data/` 不允许出现 `"/"` 占位符、`description_raw`、`source_meta`、`field_source_policy` 等中间字段
- 旧目录结构不再是稳定接口，不保留兼容层

## 当前抓取口径

- 职业图当前固定使用 `cover.*` 作为默认主图文件名
- 天赋图标 manifest 坐标标准为 `3 列 x 8 行`，按 `column-major` 编号
- `职业图片清单.json` 与 `按职业分组天赋图标.json` 由抓取脚本直接刷新，供后续 merge 使用
- Wiki 结构抓取当前仍由 JS 实现主体承担；Python 入口负责稳定命令口径，后续逐步完成单运行时迁移
