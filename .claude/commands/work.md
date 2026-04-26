---
description: Daily short alias for read-only agent worktree scan.
---

# /work

日常短入口。语义与 `/scan-agent-worktrees` 完全一致：默认只读扫描 agent worktree / `wt-auto-*` branch 状态。

调用：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File ".claude\tools\worktree-governor\scan_agent_worktrees.ps1"
```

边界：

- 当前只扫描，不清理。
- 不删除 branch。
- 不删除 worktree。
- 不调用 `sweep -Apply`。
- 不调用 `git branch -d` 或 `git branch -D`。
- 不调用 `git worktree remove`。
- 不调用 `Remove-Item`、`rm` 或目录清理。
- 清理必须用户另行显式确认。
