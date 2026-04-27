---
description: Explicitly clean authorized clean claudecode agent worktrees.
---

# /cleanup-agent-worktrees

显式清理入口。用户运行本命令即表示授权执行受控的 clean agent worktree 清理。

调用：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File ".claude\tools\worktree-governor\cleanup_agent_worktrees.ps1"
```

边界：

- 先输出候选摘要，再执行受控清理。
- 只处理 registry 中 `owner=agent`、`protected=false` 的 worktree。
- 只处理位于 `C:\Users\apple\_worktrees\claudecode\auto\` 下的 worktree。
- 只处理 branch 匹配 `wt-auto-*` 且 `git status --porcelain` 为空的 worktree。
- 不删除 branch；branch 仍由 sweep 工具单独处理。
- 不删除 dirty worktree。
- 不删除 `owner=user` 或 `protected=true` worktree。
- 缺少 registry marker、marker 不匹配、非 auto root 或非 `wt-auto-*` branch 时只报告 skipped。
- 不使用 force worktree remove，不使用文件系统删除命令。
- 裸 `git worktree remove` 不作为默认路径。
