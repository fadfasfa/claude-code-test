---
description: 审计或清理 PR 合并后的 managed Git worktree / branch 残留
argument-hint: "[audit|--dry-run] [--no-prune] [--repo PATH] [--base REF]"
allowed-tools: [Bash]
---

执行项目 skill：`.claude/skills/cleanup-worktrees/SKILL.md`。

原始参数：`$ARGUMENTS`

默认执行安全清理：只处理 PR 已合并、相对 base 无领先提交、干净、无 untracked/ignored 本地文件、受管且非 protected 的本地 worktree / branch 残留；dirty、含 ignored 缓存、未合并、仍领先、非受管或 protected 的对象只列清单并保持不变。`audit` / `audit worktrees` / `--dry-run` / `--audit` / `只审计` 只输出清单，`--apply` 只是兼容别名。

默认可逐个删除已合并且无领先提交的 stale `origin/*` 本机缓存；只有 `git ls-remote --heads origin` 成功后才允许判定 stale，远端列表失败时跳过 remote ref 清理。不得删除 GitHub 上真实存在的远端分支。不得调用或恢复 `scripts/git/safe-worktree-cleanup.ps1`；不得升级到 force remove、`branch -D`、未限定 remote 的 prune、`git clean`、`reset --hard`、push、PR、merge、rebase 或 tag。
