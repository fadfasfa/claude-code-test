---
description: 审计或清理 PR 合并后的 managed Git worktree / branch 残留
argument-hint: "[audit|--dry-run] [--no-prune] [--repo PATH] [--base REF]"
allowed-tools: [Bash]
---

执行项目 skill：`.claude/skills/cleanup-worktrees/SKILL.md`。

原始参数：`$ARGUMENTS`

清理边界必须遵守 `docs/当前规则/10-工作区登记.md`、`docs/当前规则/20-Git与高危操作.md`、`docs/当前规则/30-验证与审查.md` 和 `docs/当前规则/40-Agent与Skill.md`。

默认使用简体中文输出短执行结果和总结。

默认执行快路径安全清理：只处理 PR 已合并、相对刷新后的 base 无领先提交、无 tracked 修改、无 `??` untracked、标准 managed root、legacy cleanup-only root 或仓库内临时 review 根下且非 protected 的本地 worktree / branch 残留。legacy cleanup-only root 只包括 `C:\Users\apple\worktrees\claudecode-*`，不作为新建 worktree 目标。默认输出短摘要：`removed worktrees`、`deleted local branches`、`deleted stale origin refs`、`skipped`。

`audit` / `audit worktrees` / `--dry-run` / `--audit` / `只审计` 只输出清单。遇到 base stale、fetch 失败、dirty、`??` untracked、凭据名 ignored、非受管路径、远端列表失败或远端 ref 仍存在时，降级为重审计，输出候选表和原因码。

默认清理模式先解析 `origin/HEAD` 对应 base branch，再可执行一次定向 `git fetch --no-tags origin refs/heads/<base_branch>:refs/remotes/origin/<base_branch>`。不得删除 GitHub 上真实存在的远端分支；不得调用或恢复 `scripts/Git辅助/safe-worktree-cleanup.ps1`；不得升级到 `git worktree remove --force`、`branch -D`、`git fetch --prune`、`git remote prune origin`、未限定 remote/ref 的 prune、`git clean`、`reset --hard`、push、PR、merge、rebase 或 tag。

如用户显式给出 `--no-prune`，跳过 stale `origin/*` 本机缓存清理。
