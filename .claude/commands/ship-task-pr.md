---
description: Ship current task changes through a controlled branch, commit, push, and PR wrapper.
argument-hint: "[--branch <branch-name>] --title \"<title>\" | <title>"
---

# /ship-task-pr

受控远端写入入口，用于把本次任务改动整理成新分支、提交、推送到 `origin` 并创建 PR。

调用：

```text
/ship-task-pr <title>
/ship-task-pr --branch <branch-name> --title "<title>"
```

执行 wrapper：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File ".claude\tools\pr\ship_task_pr.ps1" -Title "<title>"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File ".claude\tools\pr\ship_task_pr.ps1" -Branch "<branch-name>" -Title "<title>"
```

边界：

- 这是远端写入命令。
- 用户调用 `/ship-task-pr` 本身视为本次 `push` 和 `gh pr create` 的明确授权，但只授权这个受控 wrapper。
- 不复用 `/scan-agent-worktrees`；后者仍然只做只读扫描。
- 不允许 force push。
- 不允许 push `main` 或 `master`。
- 不允许删除 branch 或 worktree。
- 不允许 `git reset --hard` 或 `git clean`。
- 不允许把 `.claude/settings.local.json`、`.tmp/**`、日志文件、`node_modules/**` 或 `.venv/**` 纳入提交。
- 检查 diff、PR body 或上下文时，不调用原生 `Read` 读取 text/code 文件；使用 Git diff、Grep / Glob / Bash。
- 裸 `git push` 仍不默认放开。
- 不自动打开浏览器。
