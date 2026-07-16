---
description: 审计或清理 PR 合并后的 managed Git worktree / branch 残留
argument-hint: "[audit|--dry-run] [--no-prune] [--repo PATH] [--base REF]"
allowed-tools: [Bash]
---

执行项目 skill：`.claude/skills/cleanup-worktrees/SKILL.md`。

原始参数：`$ARGUMENTS`

清理边界必须遵守 `docs/当前规则/10-工作区登记.md`、`docs/当前规则/20-Git与高危操作.md`、`docs/当前规则/30-验证与审查.md` 和 `docs/当前规则/40-Agent与Skill.md`。

默认使用简体中文输出短执行结果和总结。

默认执行快路径安全清理：祖先合并沿用 `merge-base --is-ancestor` 与 ahead=0；非祖先候选必须按项目 skill 交叉验证唯一 merged PR、base/head 名称、PR head OID、本地候选 OID 和已进入 base 的 merge commit，才可标记 `squash-merged-pr`。默认输出短摘要：`removed worktrees`、`deleted local branches`、`deleted stale origin refs`、`skipped`。

`audit` / `audit worktrees` / `--dry-run` / `--audit` / `只审计` 只输出清单且不得 fetch。dirty 内容不得自动丢弃；PR 元数据不可用、结果不唯一、OID 不一致或 merge commit 未进入 base 时保持候选不变并输出原因码。

默认清理模式可定向刷新 base。已验证 squash 分支只允许用带 expected-old-OID 的 `git update-ref -d` 原子删除；不得删除 GitHub 上真实存在的远端分支，不得升级到 `git worktree remove --force`、`branch -D`、无 expected-old-OID 的 ref 删除、`git fetch --prune`、`git remote prune origin`、`git clean` 或 `reset --hard`。

如用户显式给出 `--no-prune`，跳过 stale `origin/*` 本机缓存清理。
