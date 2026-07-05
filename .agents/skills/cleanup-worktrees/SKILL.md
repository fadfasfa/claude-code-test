---
name: cleanup-worktrees
description: Use when the user asks to run cleanup-worktrees, $cleanup-worktrees, audit worktrees, or clean PR-merged managed or legacy claudecode Git worktree, branch, and stale origin/* residue in this repository.
---

# cleanup-worktrees

本 skill 是 `claudecode` 仓库的 Codex 对话入口。默认走快路径清理 PR 合并后的本地残留；完整安全合同以 `docs/当前规则/20-Git与高危操作.md` 为准。

## 先读边界

- 必须遵守 `docs/当前规则/10-工作区登记.md`、`docs/当前规则/20-Git与高危操作.md`、`docs/当前规则/30-验证与审查.md` 和 `docs/当前规则/40-Agent与Skill.md`。
- 标准 root：`C:\Users\apple\worktrees\codex`、`C:\Users\apple\_worktrees\cc`。
- cleanup-only legacy root：`C:\Users\apple\worktrees\claudecode-*`；只清理历史平铺 worktree，不作为新建目标。
- 仓库内临时 review root：`.claude/worktrees/agent-*`。

## 默认快路径

1. 运行 `git status --short --untracked-files=all`、`git worktree list --porcelain`、`git for-each-ref --format='%(refname:short) %(upstream:short) %(upstream:track)' refs/heads/`。
2. 解析 `origin/HEAD` 对应 base branch；默认清理模式可定向刷新：`git fetch --no-tags origin refs/heads/<base_branch>:refs/remotes/origin/<base_branch>`。
3. 对候选 worktree：路径必须在允许 root 内，`git status --short --untracked-files=all` 必须为空，ignored 输出不得命中凭据名。
4. 对候选 branch / HEAD：`git merge-base --is-ancestor <branch-or-head> <base>` 必须成功，且 `git rev-list --left-right --count <base>...<branch-or-head>` 右侧必须为 `0`。
5. 符合条件时执行普通 `git worktree remove <path>`、`git branch -d <branch>`；stale `origin/*` 只在 `git ls-remote --heads origin` 成功且远端真实不存在后逐项 `git branch -dr origin/<branch>`。
6. 如用户显式给出 `--no-prune`，跳过 stale `origin/*` 本机缓存清理。

## 何时展开重审计

用户显式 `audit` / `--dry-run` / `只审计`，或遇到以下异常时，输出候选表和原因码，不执行对应清理：

- base stale 且审计模式不得 fetch：`base-stale-needs-fetch` / `needs-fetch-for-cleanup`。
- fetch 失败：`fetch-failed-skip`。
- dirty、tracked 修改、`??` untracked、ignored 凭据名。
- 路径不在允许 root：`outside-managed-root`。
- 分支绑定非候选 worktree：`bound-to-noncandidate-worktree`。
- 远端列表失败或远端 ref 仍存在：`remote-list-failed` / `remote-ref-still-exists`。

## 禁止事项

- 审计模式不得删除、移动、prune、fetch、stage、commit 或 push。
- 不使用 `git worktree remove --force`、`branch -D`、`git fetch --prune`、`git remote prune origin`、未限定 remote/ref 的 prune、`git clean`、`reset --hard`。
- 不删除 GitHub 上真实存在的远端分支。
- 不读取或修改凭据、token、cookie、API key、proxy secret、`.env`、`auth.json`、`local.yaml`、`proxies.json`、`accounts.json`。

## 输出

- 默认清理模式输出短摘要：`removed worktrees`、`deleted local branches`、`deleted stale origin refs`、`skipped`。
- 审计模式或异常降级时输出候选表和原因码。
- 收尾说明是否定向 fetch、是否移除 worktree、是否删除本地分支、是否删除 stale `origin/*` 本机缓存、是否 stage/commit/push。
