---
name: cleanup-worktrees
description: Use when the user invokes /cleanup-worktrees or asks Claude Code to audit worktrees, or clean PR-merged managed or legacy claudecode Git worktree, branch, and stale origin/* residue in this repository.
---

# cleanup-worktrees

本 skill 是 `claudecode` 仓库的 Claude Code 对话入口。默认清理经本轮审计确认安全的 PR 合并残留；完整安全合同以 `docs/当前规则/20-Git与高危操作.md` 为准。

## 先读边界

- 必须遵守 `docs/当前规则/10-工作区登记.md`、`docs/当前规则/20-Git与高危操作.md`、`docs/当前规则/30-验证与审查.md` 和 `docs/当前规则/40-Agent与Skill.md`。
- managed roots：`C:\Users\apple\worktrees\codex`、`C:\Users\apple\_worktrees\cc`。
- cleanup-only legacy root：`C:\Users\apple\worktrees\claudecode-*`；只清理历史平铺 worktree，不作为新建目标。
- transient review root：仓库内 `.claude/worktrees/agent-*`。
- protected branch：`main`、`master`、`develop`、`dev`、`release/*`、`hotfix/*`、`prod/*`。

## 审计与合并判定

1. 运行 `git status --short --untracked-files=all`、`git worktree list --porcelain`、`git for-each-ref --format='%(refname:short) %(upstream:short) %(upstream:track)' refs/heads/`。
2. 解析 `origin/HEAD` 对应 base branch。默认清理模式可定向刷新：`git fetch --no-tags origin refs/heads/<base_branch>:refs/remotes/origin/<base_branch>`；审计模式不得 fetch。
3. 对每个候选解析固定 `candidate_oid`。先运行 `git merge-base --is-ancestor <candidate_oid> <base>`，并用 `git rev-list --left-right --count <base>...<candidate_oid>` 验证 ahead=0；通过时记为祖先合并。
4. 非祖先候选才调用 `gh pr list --state merged --head <branch> --limit 100 --json number,state,mergedAt,mergeCommit,headRefName,headRefOid,baseRefName`。只保留同时满足以下条件的记录：
   - 返回记录唯一，`state=MERGED` 且 `mergedAt` 非空；
   - `baseRefName` 等于解析后的 base branch，`headRefName` 等于候选分支；
   - `headRefOid` 等于固定的 `candidate_oid`；
   - `mergeCommit.oid` 非空，且 `git merge-base --is-ancestor <mergeCommit.oid> <base>` 成功。
5. 全部通过时记为 `squash-merged-pr` 并记录 PR 编号。不得使用 patch-id、文件 diff 相似度或提交标题猜测 squash 状态。
6. `gh` 不可用、未认证、查询失败或字段缺失时记为 `pr-metadata-unavailable`；多条记录满足时记为 `ambiguous-merged-pr`；head OID 不一致时记为 `pr-head-oid-mismatch`；merge commit 未进入 base 时记为 `merge-commit-not-in-base`。这些结果均保持候选不变。

## 工作树与 ignored 内容

- 路径必须在允许 root 内，且非 locked、bare、protected/current worktree。
- 先用 `git status --short --untracked-files=all` 判断硬干净状态；tracked、staged 或 `??` untracked 任一存在即阻断。
- 再用 `git status --short --untracked-files=all --ignored=matching` 记录 ignored 摘要。普通 runtime/cache/log/data 的 `!!` 输出只报告，不单独阻断。
- ignored 输出命中凭据或登录态名称时跳过，且不得读取内容。
- Skill 不自动丢弃 dirty 内容。只有用户当前轮精确授权具体路径后，才可丢弃该路径并从第一步重新完整审计。

## 默认清理

- `audit`、`--dry-run`、`--audit`、`只审计`、`只看` 或 `列清单` 只输出审计结果，不 fetch、不删除。
- 对通过审计的硬干净 worktree，只执行普通 `git worktree remove <path>`。
- 祖先合并分支仍用 `git branch -d <branch>`。
- 已验证的 squash 分支仅在 worktree 移除成功且分支不再绑定其他 worktree 后，执行 `git update-ref -d refs/heads/<branch> <candidate_oid>`。expected-old-OID 不匹配时记为 `ref-changed-before-delete` 并保留分支。
- stale `origin/*` 只有在 `git ls-remote --heads origin` 成功、远端真实不存在且祖先或 squash 合并证据通过后，才逐项执行 `git branch -dr origin/<branch>`。
- `--no-prune` 跳过 stale `origin/*` 本机缓存清理。

## 禁止事项

- 不使用 `git worktree remove --force`、`branch -D`、无 expected-old-OID 的 `git update-ref -d`、`git fetch --prune`、`git remote prune origin`、未限定 remote/ref 的 prune、`git clean` 或 `reset --hard`。
- 不删除 GitHub 上真实存在的远端分支，不调用或恢复已退役的清理脚本。
- 不读取或修改凭据、token、cookie、API key、proxy secret、`.env`、`auth.json`、`local.yaml`、`proxies.json`、`accounts.json`。

## 输出

- 默认清理模式输出 `removed worktrees`、`deleted local branches`、`deleted stale origin refs`、`skipped`。
- 审计模式或异常降级时输出候选表，区分“祖先合并”和“GitHub 已验证 squash 合并”，并报告 PR 编号、结论和原因码。
- 收尾说明是否定向 fetch、是否移除 worktree、是否删除本地分支或 stale `origin/*`、是否 stage/commit/push。
