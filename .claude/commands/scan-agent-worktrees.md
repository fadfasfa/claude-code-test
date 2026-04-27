---
description: Authorized scan and cleanup of claudecode agent worktrees and wt-auto branches.
---

# /scan-agent-worktrees

唯一 agent worktree / `wt-auto-*` branch 清理入口。用户运行本命令即表示授权执行完整清理。

调用：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File ".claude\tools\worktree-governor\scan_agent_worktrees.ps1"
```

执行内容：

- 扫描 agent worktree 和 `wt-auto-*` branch。
- 删除 clean `owner=agent` auto worktree。
- 删除 zero-ahead `wt-auto-*` `owner=agent` branch。
- 不再需要单独 `/cleanup-agent-worktrees`。
- 不在中途追加 PowerShell / Bash 确认。

边界：

- 不删除 dirty worktree。
- 不删除 `owner=user` 或 `protected=true` worktree / branch。
- 不删除非 auto root worktree。
- 不删除缺少 registry marker 的 worktree / branch。
- 不删除有 unique commits、正在被 worktree checkout、配置 upstream 或存在 `origin/<branch>` 的 branch。
- 不执行 `git branch -D`。
- 不执行 `Remove-Item`、`rm` 或目录清理。
