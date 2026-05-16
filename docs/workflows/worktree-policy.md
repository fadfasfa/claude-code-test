# Worktree Policy

## 默认

- 默认使用单一 active worktree。
- 不并发开启多个任务分支。
- 不自动创建长期 worktree。
- `main` 只作为基准，不作为默认任务执行面。
- 写入任务默认不自动创建 worktree；只有用户显性触发或上游 plan 明确 `requires_worktree: true` 时，才创建 detached active worktree。

## 创建前检查

- 检查主仓 `git status --short`。
- 检查 `git worktree list --porcelain`、`C:\Users\apple\worktrees`、`C:\Users\apple\_worktrees` 和 `.task-worktree.json`。
- 目标任务必须有明确 `target_work_area`。
- 若已有 dirty active worktree，停止，不创建第二个。
- 若任务目标路径与主仓 dirty 文件重叠，停止并让用户选择：先处理主仓脏改、授权复制指定脏改，或显式 `-AllowDirtyOverlapFromHead` 从 HEAD 继续。
- 创建必须使用 `git worktree add --detach`，不得依赖不传 `-b` 的默认行为。
- 创建优先落在 `C:\Users\apple\worktrees`；若创建失败，再尝试 `C:\Users\apple\_worktrees`。
- 两个 worktree 根都失败时必须停止并报告原因，不得回到主仓编码。

## Task Metadata

- 创建 worktree 时必须生成 `TASK_HANDOFF.md` 和 `.task-worktree.json`。
- `.task-worktree.json` 必须包含固定 schema：`schema_version`、`repo_name`、`main_repo_path`、`worktree_path`、`task_slug`、`target_paths`、`base_ref`、`base_commit`、`mode`、`main_dirty_snapshot`、`acceptance_gate`、`manual_required`、`manual_accepted`、`review_branch`、`created_at`、`updated_at`。
- 缺字段时 status、review、finalize、cleanup 入口必须失败。

## 清理规则

- 只清理已完成、干净、可确认不再需要的 worktree。
- 不删除未提交改动。
- 不删除主工作树。
- 清理失败立即停止。
