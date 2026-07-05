# seed/startup

本目录是打包首启种子快照事实源。

当前构建期事实源是 `synergy/Champion_Synergy_*.json` 和 `synergy/Champion_Synergy_latest.v1.json`。
打包产物内的目标路径使用 `data/seed/startup/synergy/`；Hextech CSV seed 使用
`data/seed/startup/hextech/`。

维护约束：

- 首启快照是构建期输入，不是运行态缓存。
- 运行后生成的新快照仍属于 `data/runtime` 或用户运行目录，不进入本仓稳定资源。
- 调整文件名或指针 schema 时必须同时检查 `tools/package_rules.py`、`tools/bundle_manifest.py` 和 `tools/runtime_bundle.py`。
