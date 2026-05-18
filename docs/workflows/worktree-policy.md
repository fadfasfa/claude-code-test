# Worktree Policy

本文件只描述当前 worktree 规则。普通任务默认在当前工作树小步执行，不自动创建分支或 worktree。

## Trigger

只有以下情况才进入 worktree 流程：

- 用户明确要求开 worktree。
- 上游任务明确标注 `requires_worktree: true`。
- 用户在后续任务中显式提供新的 worktree 创建入口和授权。

多文件、多阶段、高风险或 non-trivial 本身都不构成开树触发。

## Create

创建前必须只读检查：

- `git status --short`
- `git worktree list --porcelain`
- `C:\Users\apple\worktrees`
- `C:\Users\apple\_worktrees`
- 目标任务的 `target_work_area`

旧 `scripts/workflow/worktree-start.ps1` 已随旧 workflow 主流程移除。未建立新入口前，不自动创建 detached worktree，也不默认写入 `TASK_HANDOFF.md` 与 `.task-worktree.json`。

若存在 dirty active worktree、目标路径与主仓脏改重叠，或两个受管根都创建失败，立即停止。

## Metadata And Cleanup

- `TASK_HANDOFF.md` 和 `.task-worktree.json` 只属于显式 worktree 流程。
- 普通任务不生成、不更新这些文件。
- 旧 metadata / cleanup 脚本已随旧 workflow 主流程移除；普通任务不读取或更新 worktree metadata。
- 真实清理前必须确认目标受管且工作树干净，并获得用户显性授权。
