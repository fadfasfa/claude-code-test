---
name: repo-maintenance
description: 用于本仓健康检查、维护候选、受保护资产检查和脚本状态核对；默认只读或 dry-run。worktree 的审计和真实清理由 cleanup-worktrees 负责。
---

# repo-maintenance

## trigger

- 用户要求维护候选、健康检查或脚本状态检查。
- 需要确认受保护资产、备份、安全边界或脚本状态。

## scope

- 默认使用简体中文输出事实、候选动作、风险、验证和总结。
- 优先使用只读 Git 检查、`scripts/Git辅助/ccw-ls.ps1` 和 dry-run 脚本。
- 检查 `docs/当前规则/10-工作区登记.md`、`docs/当前规则/20-Git与高危操作.md`。
- 维护建议必须区分事实、候选动作和需要授权动作。
- worktree、branch 或 stale origin ref 的审计与清理转交 `cleanup-worktrees`，本 skill 不复制其判断逻辑。

## action boundary

- 默认不删除未提交改动，不清理任何业务工作区当前脏树，不改 ACL，不执行 git clean、reset、stash、rebase。
- 未获当前任务授权时，不触碰 `run/**`、`sm2-randomizer/**`、`sms-monitor/**`、`heybox/**`、`subtitle_extractor/**`、`QuantProject/**` 或 `qm-run-demo`。
- 用户明确授权指定 clean、reset、discard、stash 或 rebase 时，先做只读目标核对，再按 `AGENTS.md` 的授权范围执行并验证。
- 不触碰凭据、token、cookie、auth、proxy secret。

## verification expectation

- 清理前必须证明目标路径和状态。
- 需要备份时，先验证备份成功；备份失败即停止。
- 默认报告 dry-run 结果，不宣称已经清理。

