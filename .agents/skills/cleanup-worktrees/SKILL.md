---
name: cleanup-worktrees
description: Use when the user asks to run cleanup-worktrees, /cleanup-worktrees, $cleanup-worktrees, audit worktrees, or clean merged managed Git worktrees in this claudecode repository.
---

# cleanup-worktrees

本 skill 是 `claudecode` 仓库的 Codex 对话入口。默认只做审计；只有用户当前消息明确包含 `--apply`、`apply`、`执行清理`、`删除`、`真的清理` 或同等清理授权时，才允许执行删除。一次清理授权即覆盖本轮所有清理动作（worktree、孤立已合并本地分支、stale 远程缓存），无需再次授权。

## Scope

- repo：默认当前仓库；如用户提供 `--repo PATH`，先确认该路径是 Git 仓库。
- base：默认 `origin/HEAD`；如用户提供 `--base REF`，使用该 ref。
- managed worktree roots：`C:\Users\apple\worktrees\codex` 与 `C:\Users\apple\_worktrees\cc`。
- 顺带清理对象（apply 模式默认覆盖，无需再次授权）：
  - 已合并到 base 的孤立本地分支（无 worktree 绑定、非 protected）。
  - stale 远程跟踪缓存（`refs/remotes/origin/*` 在远端已删，本地仍存）。

## Required Audit

1. 先运行 `git status --short --untracked-files=all`。
2. 运行 `git worktree list --porcelain`。
3. 运行 `git for-each-ref --format='%(refname:short) %(upstream:short) %(upstream:track)' refs/heads/`，定位 `[gone]` 标记和无 upstream 的孤立分支。
4. 用 `git merge-base --is-ancestor` 判定每个孤立分支是否已并入 base。
5. 列出 `refs/remotes/origin/*` 中状态为 `[gone]` 或 `git ls-remote --heads origin` 已不存在的 stale 引用。
6. 输出三张表：
   - **worktree**：`path`、`branch`、`decision`、`reason`。
   - **orphan branches**：`branch`、`merged-to-base`、`decision`、`reason`。
   - **stale remote refs**：`ref`、`decision`、`reason`。

## Skip Rules

worktree 跳过：main/current、dirty、locked、bare、detached、protected branch、未合并到 base、路径缺失、reparse point、branch/base 无法解析。

孤立本地分支跳过：仍绑定 worktree、未合并到 base、protected branch、当前 HEAD 分支、无法解析。

remote ref 跳过：远端实际仍存在、protected branch 对应 ref、`origin/HEAD` 自身。

protected branch 包括 `main`、`master`、`develop`、`dev`、`release/*`、`hotfix/*`、`prod/*`。

## Apply Rules

- 审计模式不得删除、移动、prune、fetch、stage、commit 或 push。
- apply 模式按顺序执行（每步只对本轮表中标为候选的对象生效）：
  1. `git worktree remove <path>` 处理 worktree 候选。
  2. `git branch -d <branch>` 处理 worktree 绑定分支与孤立已合并分支（合并复用同一命令，仍要求 `-d` 而非 `-D`）。
  3. `git fetch --prune` 清掉 stale 远程缓存。
- `--prune` 参数已并入 apply 模式默认行为，用户无需另行声明；如果用户显式给出 `--no-prune`，则跳过第 3 步。
- 失败时不得升级到 `--force`、`branch -D`、`git clean`、`reset --hard`、强制 checkout/switch、改 remote、push、PR、merge、rebase 或 tag。
- 不调用或恢复 `scripts/git/safe-worktree-cleanup.ps1`；该 CLI 入口已退役。
- 不读取或修改凭据、token、cookie、API key、proxy secret、`.env`、`auth.json`、`local.yaml`、`proxies.json`、`accounts.json`。

## Output

先给三张审计表。若 apply，逐项报告执行结果（worktree 移除、本地分支删除、prune 输出）；若任一类没有候选，明确说明未执行。收尾必须说明：是否移除 worktree、是否删除孤立本地分支、是否执行 prune、是否 stage/commit/push。
