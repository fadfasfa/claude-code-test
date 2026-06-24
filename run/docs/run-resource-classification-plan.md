# run 资源中文分类、迁移与目录收口记录

## 目标

用中文二级目录承载 `run/resources/` 下的稳定资源事实源，降低 `assets/`、
`data/static/`、`data/indexes/` 和 `data/raw/` 混杂带来的维护成本。

重构后的事实源口径：

- `resources/图片资源/`：随包分发的图片和图标资源。
- `resources/版本数据/`：版本级稳定 JSON / TXT 数据。
- `resources/首启快照/`：构建期可读取、用于首次启动播种的快照。
- `resources/诊断样例/`：离线视觉匹配和诊断回归样例。
- `resources/来源证据/`：用于复现清洗结果的外部来源原始输入。

## 边界

- 一级目录继续保持英文，避免破坏 Python import、打包脚本和已有路径约定。
- 中文目录只作为 `resources/` 下的二级维护分类入口。
- `data/runtime/**` 只属于运行态输出，不进入稳定资源清单。
- 运行期生成的 `data/raw/**` 不归类为普通版本数据；需要保留时先判断是来源证据还是首启快照。
- 旧 Web 路由 `/assets/...`、`/data/static/...` 和 `/data/indexes/...` 只保留兼容语义，不再代表源码态事实源目录。

## 已完成阶段

1. 建立中文分类清单和说明文档。
   - 新增 `resources/资源清单.v1.json`。
   - 新增各中文分类目录的 `README.md`。
   - 接入资源清单校验，确保分类名和目录存在。
2. 迁移稳定资源到中文二级目录。
   - 根级 `assets/*.png` 迁入 `resources/图片资源/`。
   - `data/static/` 与 `data/indexes/` 的稳定数据迁入 `resources/版本数据/`。
   - 首启协同快照、视觉诊断样例和来源证据分别迁入对应中文目录。
   - 同步加载器、Web 兼容路由、打包白名单、runtime bundle 和文档。
3. 合并过度拆分的版本 JSON。
   - 英雄别名、alias-to-id、id-to-name、id-to-detail 收口为 `resources/版本数据/英雄目录.v1.json`。
   - 海克斯 manifest、name-to-icon、apexlol slug map 收口为 `resources/版本数据/海克斯资源目录.v1.json`。
   - 旧拆分文件名由兼容投影提供，不再作为源码态文件维护。
4. 收口兼容入口和验证。
   - `/data/static/...` 与 `/data/indexes/...` 由受控 API 路由提供兼容响应。
   - `/assets/...` 仍暴露图片 URL，但源码态图片来自 `resources/图片资源/`。
   - `load_default_template_index()` 在默认路径和显式 `run` 根路径下都读取新目录。

## 后续维护要求

- 新增稳定资源时，先判断资源属于哪个中文分类，再同步 `resources/资源清单.v1.json`。
- 不新增 `data/static/` 或 `data/indexes/` 作为源码态目录；如需兼容旧 URL，应在路由层投影。
- 不把运行态日志、cache、profile、lock 或本机状态写入 `resources/`。
- 如需继续合并 JSON，优先在现有目录文件中增加字段，并保留明确 schema/version 字段。
- 更新路径时必须同步加载器、打包白名单、bundle manifest、runtime bundle、离线验收脚本和文档。

## 验证入口

完成资源路径或目录 JSON 调整后，至少运行：

```powershell
python tools/dev_checks.py
python tools/dev_checks.py --bundle-manifest
git diff --check
```
