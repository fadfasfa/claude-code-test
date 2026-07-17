# Stable Assets

本目录按领域保存随包分发的稳定图片：`champions/`、`augments/`、
`modes/mayhem/`、`modes/swarm/` 和 `ui/`。Web 只按同样的层级 URL 提供
只读访问，不支持旧平铺路径。

只允许构建白名单中的图片类型；运行期下载、截图、缓存和诊断产物写入 `var/`。
调整资源规则时同步检查 `tooling/build/rules.py`、`tooling/build/manifest.py` 和
`tooling/build/runtime_bundle.py`。
