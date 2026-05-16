# workflow 脚本说明

本目录是当前仓库级 workflow 执行脚本层。脚本默认不读取凭据文件，不执行发布动作；涉及写入、删除、worktree 或 Git 状态变更时必须通过参数显式触发。

| 脚本 | 作用 | 写入行为 |
| :--- | :--- | :--- |
| `cx-exec.ps1` | CC -> Codex 真实 executor；根目录 `cx-exec.ps1` 只负责转发 | 写 `.state/workflow/tasks/<task_id>/` |
| `verify.ps1` | 最小验证入口 | 只读或调用子验证 |
| `local-review.ps1` | 本地 diff 审查和 acceptance gate 摘要 | 更新 `TASK_HANDOFF.md` / `.task-worktree.json` |
| `worktree-start.ps1` | 手动创建单一 detached active worktree | 默认 dry-run；`-Apply` 才创建 |
| `worktree-status.ps1` | 输出当前 worktree 与 task metadata 状态 | 只读 |
| `finalize-pr.ps1` | 发布前 dry-run / finalize 检查 | 默认 dry-run；commit/push 需显式授权 |
| `cleanup-worktree.ps1` | 清理已完成 worktree | 默认 dry-run；真实清理需显式参数和保护检查 |
| `task-metadata.ps1` | 共享 task metadata 校验函数 | 被其他脚本 dot-source |
| `tests/cx-exec.Tests.ps1` | CC -> CX 入口静态测试 | 只读 |

当前运行态根是 `.state/workflow/`；不要让 workflow 脚本重新创建 `run/workflow/`、`.workflow/` 或 `.codex-exec-apple/`。
