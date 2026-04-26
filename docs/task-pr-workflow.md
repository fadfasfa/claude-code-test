# 任务 PR 发货工作流

本文件说明 `claudecode` repo-local 的 `/ship-task-pr` 命令。它是显式远端写入入口，只用于把当前任务改动通过受控 wrapper 整理成 branch、commit、push 和 PR。

## 命令入口

- `/scan-agent-worktrees`：只读扫描 agent worktree / `wt-auto-*` branch。它不删除 branch，不删除 worktree，不运行 `sweep -Apply`。
- `/ship-task-pr`：显式远端写入入口。用户调用该命令本身视为本次 `push` 和 `gh pr create` 的授权，但只能通过 `.claude\tools\pr\ship_task_pr.ps1` 执行。

调用形式：

```text
/ship-task-pr <title>
/ship-task-pr --branch <branch-name> --title "<title>"
```

wrapper：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File ".claude\tools\pr\ship_task_pr.ps1" -Title "<title>"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File ".claude\tools\pr\ship_task_pr.ps1" -Branch "<branch-name>" -Title "<title>"
```

## 安全边界

- 不允许 force push。
- 不允许 push `main` 或 `master`。
- 不允许删除 branch 或 worktree。
- 不允许 `git reset --hard` 或 `git clean`。
- 不允许把 `.claude/settings.local.json`、`.tmp/**`、日志文件、`node_modules/**` 或 `.venv/**` 纳入提交。
- 裸 `git push` 仍不默认放开；只允许 wrapper 内部执行 `git push -u origin <branch>`。
- PR 创建必须使用非交互式 `gh pr create --body-file`，不自动打开浏览器。

## 暂存策略

如果已经存在 staged 文件，wrapper 只提交这些 staged 文件，并拒绝 blocked paths。

如果没有 staged 文件，wrapper 只会自动 stage 高置信 repo-governance / repo-local tooling 路径，例如 `.claude/commands/**`、`.claude/tools/**`、`docs/**`、`agent_tooling_baseline.md` 和入口文档。遇到无法判断是否属于当前任务的 unstaged 文件时会停止，并要求用户先手动 stage。

## PR body

wrapper 会把 PR body 写到 `.tmp/pr/<branch>/body.md`。`.tmp/**` 是 ignored runtime 输出，不能纳入提交。

PR body 包含：

- `Summary`：根据提交文件分组生成，最多 3 条。
- `Test plan`：记录 wrapper 实际运行的 `git diff --cached --check`；项目测试不由 wrapper 推断，未记录时明确写 `Not run`。
- `Changed files`：列出提交文件。
