# Worktree Policy

## 默认

- 默认使用单一 active worktree。
- 不并发开启多个任务分支。
- 不自动创建长期 worktree。
- `main` 只作为基准，不作为默认任务执行面。

## 创建前检查

- 检查主仓 `git status --short`。
- 检查是否已有 active worktree。
- 目标任务必须有明确 `target_work_area`。
- 若已有未提交改动，先判断是否与本轮相关。

## 清理规则

- 只清理已完成、干净、可确认不再需要的 worktree。
- 不删除未提交改动。
- 不删除主工作树。
- 清理失败立即停止。
