# workflow scripts

本目录是当前仓库级 workflow 脚本层。涉及写入、删除、worktree 或 Git 状态变更时都必须显式传参触发。

| 脚本 | 作用 | 默认行为 |
| :--- | :--- | :--- |
| `cx-exec.ps1` | CC -> Codex executor；根目录 `cx-exec.ps1` 只负责转发 | 写 `.state/workflow/tasks/<task_id>/` |
| `verify.ps1` | 最小验证入口 | 只读或调用子验证 |
| `local-review.ps1` | 本地 diff 审查摘要 | 需要 task metadata 时更新显式 worktree 文件 |
| `worktree-start.ps1` | 手动创建 detached worktree | 默认 dry-run；`-Apply` 才创建 |
| `worktree-status.ps1` | 输出 worktree 与 task metadata 状态 | 只读 |
| `finalize-pr.ps1` | 发布前 dry-run / finalize 检查 | 默认 dry-run；commit/push 需单独授权 |
| `cleanup-worktree.ps1` | 清理已完成 worktree | 默认 dry-run；真实清理需显式参数和保护检查 |
| `task-metadata.ps1` | task metadata 校验函数 | 被其他脚本 dot-source |
| `tests/cx-exec.Tests.ps1` | `cx-exec` 静态测试 | 只读 |

当前运行态根是 `.state/workflow/`。普通任务不生成 `docs/plans/*.md`、Markdown report、probe 或 archive 证据文件。
