# Hextech 稳定资源

本目录是稳定只读资源边界：只放可随包分发、可由 manifest 明确枚举的资源。

打包用首启快照写入 bundle 内 `resources/snapshots/`。构建期可以从仓库
`data/raw/` 读取最新可用快照，但 manifest 和打包产物不得包含 `data/raw/`
路径。首次启动时由 `tools/runtime_bundle.py` 播种回用户运行目录。

源码态稳定资源事实源已收口到 `resources/` 下的中文二级目录。Web URL、打包目标和
部分兼容路由仍可保留旧语义，例如 `/assets/...`、`/data/static/...` 和
`/data/indexes/...`。

## 中文二级分类

当前中文维护分类：

- `图片资源/`：图片和图标事实源。
- `版本数据/`：版本级稳定 JSON / TXT 数据事实源。
- `首启快照/`：构建期可读取的首启种子。
- `诊断样例/`：overlay 视觉离线回归样例。
- `来源证据/`：用于复现清洗结果的 raw 输入。

分类事实源见 `资源清单.v1.json`。后续若继续合并 JSON 或调整包内目标路径，必须同步加载路径、
打包白名单和验收脚本。

不得把 `data/raw/`、`data/runtime/`、缓存、锁、日志、profile 或抓取产物放入这里。
