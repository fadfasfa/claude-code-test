# 首启快照

本目录是打包首启种子快照的中文事实源。

当前构建期事实源是 `Champion_Synergy_*.json` 和 latest 指针。打包产物内的目标路径
仍保持 `resources/snapshots/synergy/` 语义，不把源码态中文目录名暴露为运行态路径。

维护约束：

- 首启快照是构建期输入，不是运行态缓存。
- 运行后生成的新快照仍属于 `data/runtime` 或用户运行目录，不进入本仓稳定资源。
- 调整文件名或指针 schema 时必须同时检查 `tools/package_rules.py`、`tools/bundle_manifest.py` 和 `tools/runtime_bundle.py`。
