---
name: cleanup-worktrees
description: Use when the user invokes /cleanup-worktrees or asks Claude Code to audit or clean merged managed Git worktrees in this claudecode repository.
---

# cleanup-worktrees

本 skill 是 `claudecode` 仓库的 Claude Code 对话入口。默认清理 PR 合并后的本地残留：已合并到 base、相对 base 无领先提交、无 tracked 修改、无 `??` untracked，且 ignored 输出为空或仅包含白名单 `__pycache__/` 目录条目缓存、受管且非 protected 的 worktree / 本地分支，以及已并入 base 的 stale `origin/*` 本机缓存。未合并、仍领先、dirty、存在 `??` untracked、存在非白名单 ignored 文件或目录、非受管、protected 或远端仍真实存在的对象只列清单并保持不变。

## Scope

- repo：默认当前仓库；如用户提供 `--repo PATH`，先确认该路径是 Git 仓库。
- base：默认 `origin/HEAD`；如用户提供 `--base REF`，使用该 ref。
- managed worktree roots：`C:\Users\apple\worktrees\codex` 与 `C:\Users\apple\_worktrees\cc`。
- 默认模式：执行安全清理；`--apply` 只是兼容别名。
- 审计模式：参数或用户措辞含 `audit`、`audit worktrees`、`--dry-run`、`--audit`、`dry-run`、`审计`、`只审计`、`只看` 或 `列清单` 时，只输出清单，不删除。
- `--no-prune`：跳过 stale `origin/*` 本机缓存清理。

## 白名单 ignored 缓存

- 首版只放行 `git status --ignored=matching` 输出中的 `!! .../__pycache__/` 目录条目；状态行路径必须以 `/` 结尾。
- 不放行 `!! .../__pycache__/file.pyc`、`.pytest_cache/`、`.mypy_cache/`、`build/`、`dist/`、`node_modules/`、runtime cache/log/data 或其它 ignored 文件和目录。

## Required Audit

1. 先运行 `git status --short --untracked-files=all`。
2. 运行 `git worktree list --porcelain`。
3. 运行 `git for-each-ref --format='%(refname:short) %(upstream:short) %(upstream:track)' refs/heads/`，定位本地分支、upstream 和 `[gone]` 标记。
4. 对候选分支运行 `git merge-base --is-ancestor <branch> <base>`，并用 `git rev-list --left-right --count <base>...<branch>` 确认右侧 ahead 计数为 `0`。
5. 对每个 managed worktree 候选，在该 worktree 路径内运行 `git status --short --untracked-files=all --ignored=matching`；tracked 修改和 `??` untracked 一律视为本地内容未清空，`!!` ignored 只有在仅有 `!!` ignored 输出且全部匹配任意层级 `__pycache__/` 目录条目（状态行路径以 `/` 结尾）时才视为可丢弃 Python 缓存，其它 `!!` ignored 文件或目录一律阻断。
6. 运行 `git ls-remote --heads origin` 并确认退出码为 `0` 后，才允许判断 `refs/remotes/origin/*` 是否只是本机 stale 缓存；如远端列表失败、超时或无权限，本轮 remote ref 全部跳过清理并标明 `remote-list-failed`。
7. 输出三张表：
   - **工作树**：`路径`、`分支`、`结论`、`原因`。
   - **本地分支**：`分支`、`已合入 base`、`领先提交数`、`结论`、`原因`。
   - **远端跟踪缓存**：`ref`、`结论`、`原因`。

## Skip Rules

worktree 跳过：main/current、dirty、tracked 修改、`??` untracked、非白名单 ignored 文件或目录、locked、bare、detached、protected branch、未合并到 base、相对 base 有领先提交、路径缺失、reparse point、branch/base 无法解析、路径不在 managed roots。

本地分支跳过：仍绑定非候选 worktree、未合并到 base、相对 base 有领先提交、protected branch、当前 HEAD 分支、无法解析。

remote ref 跳过：远端列表获取失败、远端实际仍存在、未合并到 base、相对 base 有领先提交、protected branch 对应 ref、`origin/HEAD` 自身。

protected branch 包括 `main`、`master`、`develop`、`dev`、`release/*`、`hotfix/*`、`prod/*`。

## Cleanup Rules

- 审计模式不得删除、移动、prune、fetch、stage、commit 或 push。
- 默认清理模式只处理本轮表中标为 PR 合并残留的候选：
  1. managed worktree 候选：只有候选 worktree 的 `git status --short --untracked-files=all --ignored=matching` 为空，或仅有 `!!` ignored 输出且全部匹配任意层级 `__pycache__/` 目录条目（状态行路径以 `/` 结尾）时，才先执行 `git worktree remove <path>`，成功后执行 `git branch -d <branch>`；不得为清理缓存而改用 `git clean` 或 `--force`。
  2. 无 worktree 绑定的本地分支候选：执行 `git branch -d <branch>`；仍要求 `-d` 而非 `-D`。
  3. stale `origin/*` 本机缓存候选：只有 `git ls-remote --heads origin` 成功后，才逐个执行 `git branch -dr origin/<branch>`；不得用 `git remote prune origin` 盲删未逐项审计的 stale refs。
- `--apply` 不扩大范围，只表示使用默认清理模式。
- 如用户显式给出 `--no-prune`，无论其他措辞如何都跳过 stale `origin/*` 本机缓存删除。
- 失败时不得升级到 `--force`、`branch -D`、`git clean`、`reset --hard`、强制 checkout/switch、改 remote、push、PR、merge、rebase 或 tag。
- 不调用或恢复 `scripts/git/safe-worktree-cleanup.ps1`；该 CLI 入口已退役。
- 不读取或修改凭据、token、cookie、API key、proxy secret、`.env`、`auth.json`、`local.yaml`、`proxies.json`、`accounts.json`。

## Output

先给三张审计表。默认清理模式逐项报告执行结果；审计模式明确说明未删除。收尾必须说明：是否移除 worktree、是否删除本地分支、是否删除 stale `origin/*` 本机缓存、是否 stage/commit/push。
