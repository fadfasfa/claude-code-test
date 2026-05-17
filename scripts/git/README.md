# Legacy Git Scripts

`ccw-*.ps1` 是历史兼容入口，不是当前默认 workflow，也不会被自动触发。

- 默认入口仍是 `scripts/workflow/*.ps1`。
- 本目录只保留兼容旧命令、旧文档或人工临时调用的脚本。
- 创建、清理 worktree 前先做只读检查。
- 新任务不扩展本目录；后续包装、迁移或退役必须单独处理。
