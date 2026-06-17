---
description: 审计并按显性授权清理已合并且干净的 managed Git worktree
argument-hint: "[--apply] [--no-prune] [--repo PATH] [--base REF]"
allowed-tools: [Bash]
---

执行项目 skill：`.claude/skills/cleanup-worktrees/SKILL.md`。

原始参数：`$ARGUMENTS`

默认只做审计。只有本次参数或用户当前消息明确包含 `--apply`、`apply`、`执行清理`、`删除`、`真的清理` 或同等清理授权时，才允许执行清理。一次清理授权即覆盖本轮所有清理动作：移除候选 worktree、删除已合并的孤立本地分支、`git fetch --prune` 清 stale 远程缓存。除非用户显式给出 `--no-prune`，否则 prune 默认执行；不再要求用户重复授权这些后续步骤。

不要调用或恢复 `scripts/git/safe-worktree-cleanup.ps1`；该 CLI 入口已退役。
