# Legacy Git Scripts

本目录中的 `ccw-*.ps1` 是历史兼容入口，不再作为默认 workflow。

- 默认入口：`scripts/workflow/*.ps1`
- 保留原因：兼容旧命令、旧文档或人工临时调用。
- 当前边界：不在新任务中扩展本目录；后续是否包装、弃用或迁移，必须作为单独任务处理。
