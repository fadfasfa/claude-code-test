---
name: repo-maintenance
description: 用于本仓维护、清理候选、受保护资产检查和工作流健康检查；默认只读或 dry-run，不删除未提交改动。
---

# repo-maintenance

## trigger

- 用户要求维护、清理候选、健康检查、worktree 状态检查。
- 需要确认受保护资产、备份、安全边界或脚本状态。

## scope

- 优先使用 `scripts/workflow/worktree-status.ps1` 和 dry-run 脚本。
- 检查 `docs/workflows/work_area_registry.md`、`docs/workflows/07-protected-assets.md`。
- 维护建议必须区分事实、候选动作和需要授权动作。

## forbidden actions

- 不删除未提交改动。
- 不清理 `run/**` 当前脏树。
- 不改 ACL。
- 不执行 git clean、reset、stash、rebase。
- 不触碰凭据、token、cookie、auth、proxy secret。

## verification expectation

- 清理前必须证明目标路径和状态。
- 需要备份时，先验证备份成功；备份失败即停止。
- 默认报告 dry-run 结果，不宣称已经清理。

