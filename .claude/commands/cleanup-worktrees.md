---
description: 审计 managed Git worktree，并按对象显性授权执行清理
argument-hint: "[--apply] [--no-prune] [--repo PATH] [--base REF]"
allowed-tools: [Bash]
---

执行项目 skill：`.claude/skills/cleanup-worktrees/SKILL.md`。

原始参数：`$ARGUMENTS`

默认只做审计。即使用户传入 `--apply`，也只能执行本次消息已经明确点名的对象类型：移除 managed worktree、删除已合并孤立本地分支、清理 origin stale 远程缓存。不要把一种清理授权扩展到其他审计表。

不得调用或恢复 `scripts/git/safe-worktree-cleanup.ps1`；不得升级到 force remove、`branch -D`、未限定 remote 的 prune、`git clean`、`reset --hard`、push、PR、merge、rebase 或 tag。
