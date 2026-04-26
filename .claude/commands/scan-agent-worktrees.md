---
description: Read-only scan of claudecode agent worktrees and wt-auto branches.
---

# /scan-agent-worktrees

只读扫描 agent worktree / `wt-auto-*` branch 状态。

必须执行：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File ".claude\tools\worktree-governor\scan_agent_worktrees.ps1"
```

边界：

- 只输出 dry-run 报告。
- 不删除 branch。
- 不执行 `git branch -d` 或 `git branch -D`。
- 不执行 `git worktree remove`。
- 不执行 `Remove-Item`、`rm` 或目录清理。
- 清理必须由用户在查看报告后再次显式确认。
