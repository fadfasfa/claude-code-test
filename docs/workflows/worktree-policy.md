# Worktree Policy

本文件只描述当前 worktree 规则。普通 Codex 任务默认在当前工作树小步执行，不自动创建分支或 worktree。

## Trigger

只有以下情况才进入 worktree 流程：

- 用户明确要求开 worktree。
- 上游任务文件明确标注 `requires_worktree: true` 或等价中文。
- 用户显式调用 `scripts/workflow/worktree-start.ps1`。

多文件、多阶段、高风险或 non-trivial 本身不构成开树触发。

## Create

创建前必须只读检查：

- `git status --short`
- `git worktree list --porcelain`
- `C:\Users\apple\worktrees`
- `C:\Users\apple\_worktrees`
- 目标任务的 `target_work_area`

创建入口是 `scripts/workflow/worktree-start.ps1`。默认 dry-run；只有显式 `-Apply` 才创建 detached worktree，并写入 `TASK_HANDOFF.md` 与 `.task-worktree.json`。

若存在 dirty active worktree、目标路径与主仓脏改重叠，或两个受管 worktree 根都创建失败，立即停止并报告原因，不回到主仓绕行编码。

## Metadata

`TASK_HANDOFF.md` 和 `.task-worktree.json` 只属于显式 worktree 流程。普通 Codex 修改任务不生成、不更新这些文件。

需要读取或校验 metadata 时使用 `scripts/workflow/task-metadata.ps1` 和 `scripts/workflow/worktree-status.ps1`。

## Cleanup

清理只通过 `scripts/workflow/cleanup-worktree.ps1`，默认 dry-run。真实清理前必须确认目标是受管 worktree、工作树干净且不再需要；不得删除主工作树或未提交改动。清理失败立即停止。
