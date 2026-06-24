# Legacy Git Scripts

`ccw-*.ps1` 是历史兼容入口，不是当前默认 workflow，也不会被自动触发。

- 旧 `scripts/workflow/*.ps1` 默认入口已移除；本目录不会成为新的默认 workflow。
- 本目录只保留兼容旧命令、旧文档或人工临时调用的查看/清理脚本。
- `ccw-new.ps1` 已移除，不再作为 active worktree 创建入口。
- 保留脚本默认只面向 Claude Code managed root：`C:\Users\apple\_worktrees\cc`。
- 创建、清理 worktree 前先做只读检查。
- 新任务不扩展本目录；后续包装、迁移或退役必须单独处理。
