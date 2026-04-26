---
description: Alias for read-only agent worktree scan.
---

# /work

`/work` 当前只是 `/scan-agent-worktrees` 的别名，用于只读扫描 agent worktree / `wt-auto-*` branch 状态。

必须执行：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File ".claude\tools\worktree-governor\scan_agent_worktrees.ps1"
```

边界：

- 当前只扫描，不清理。
- 不删除 branch。
- 不移除 worktree。
- 不删除文件或目录。
- 清理必须由用户在查看报告后再次显式确认。
