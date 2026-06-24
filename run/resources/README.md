# Hextech Stable Resources

本目录是稳定只读资源边界：只放可随包分发、可由 manifest 明确枚举的资源。

打包用首启快照写入 bundle 内 `resources/snapshots/`。构建期可以从仓库
`data/raw/` 读取最新可用快照，但 manifest 和打包产物不得包含 `data/raw/`
路径。首次启动时由 `tools/runtime_bundle.py` 播种回用户运行目录。

`assets/`、`data/static/` 和 `data/indexes/` 仍是当前稳定资源事实来源；
`resources/` 主要承载随包快照与后续可明确迁入的只读资源。

不得把 `data/raw/`、`data/runtime/`、缓存、锁、日志、profile 或抓取产物放入这里。
