# Worktree Policy

本文件只描述当前 worktree 规则。普通任务默认在当前工作树小步执行，不自动创建分支或 worktree。

## Trigger

只有以下情况才进入 worktree 流程：

- 用户明确要求开 worktree。
- 上游任务明确标注 `requires_worktree: true`。
- 用户显式调用 `scripts/workflow/worktree-start.ps1`。

多文件、多阶段、高风险或 non-trivial 本身都不构成开树触发。

## Create

创建前必须只读检查：

- `git status --short`
- `git worktree list --porcelain`
- `C:\Users\apple\worktrees`
- `C:\Users\apple\_worktrees`
- 目标任务的 `target_work_area`

创建入口是 `scripts/workflow/worktree-start.ps1`。默认 dry-run；只有 `-Apply` 才创建 detached worktree，并写入 `TASK_HANDOFF.md` 与 `.task-worktree.json`。

若存在 dirty active worktree、目标路径与主仓脏改重叠，或两个受管根都创建失败，立即停止。

## Metadata And Cleanup

- `TASK_HANDOFF.md` 和 `.task-worktree.json` 只属于显式 worktree 流程。
- 普通任务不生成、不更新这些文件。
- 读取 metadata 使用 `task-metadata.ps1` / `worktree-status.ps1`。
- 清理只通过 `cleanup-worktree.ps1`，默认 dry-run；真实清理前必须确认目标受管且工作树干净。
