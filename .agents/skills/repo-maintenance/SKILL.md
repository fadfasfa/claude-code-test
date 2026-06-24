---
name: repo-maintenance
description: 用于本仓维护、清理候选、受保护资产检查和工作流健康检查；默认只读或 dry-run，不删除未提交改动。
---

# repo-maintenance

## trigger

- 用户要求维护、清理候选、健康检查、worktree 状态检查。
- 需要确认受保护资产、备份、安全边界或脚本状态。

## scope

- 默认使用简体中文输出事实、候选动作、风险、验证和总结。
- 优先使用只读 Git 检查、`scripts/Git辅助/ccw-ls.ps1` 和 dry-run 脚本。
- 检查 `docs/当前规则/10-工作区登记.md`、`docs/当前规则/20-Git与高危操作.md`。
- 维护建议必须区分事实、候选动作和需要授权动作。

## action boundary

- 默认不删除未提交改动，不清理任何业务工作区当前脏树，不改 ACL，不执行 git clean、reset、stash、rebase。
- 未获当前任务授权时，不触碰 `run/**`、`sm2-randomizer/**`、`sms-monitor/**`、`heybox/**`、`subtitle_extractor/**`、`QuantProject/**` 或 `qm-run-demo`。
- 用户明确授权指定 clean、reset、discard、stash、rebase 或 worktree cleanup 时，先做只读目标核对，再按 `AGENTS.md` 的授权范围执行并验证。
- 不触碰凭据、token、cookie、auth、proxy secret。

## verification expectation

- 清理前必须证明目标路径和状态。
- 需要备份时，先验证备份成功；备份失败即停止。
- 默认报告 dry-run 结果，不宣称已经清理。

