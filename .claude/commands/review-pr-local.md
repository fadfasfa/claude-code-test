---
description: Run local read-only PR review against the current branch diff.
argument-hint: "[--base main|origin/main]"
---

# /review-pr-local

本命令调用 `local-pr-reviewer` agent，执行本地只读 PR 审查，替代云端 Codex PR Review。

调用：

```text
/review-pr-local
/review-pr-local --base main
/review-pr-local --base origin/main
```

默认 base 是 `main`。审查输入来自当前分支相对 base 的本地 git diff：

```powershell
git status --short
git rev-parse --abbrev-ref HEAD
git diff --stat <base>...HEAD
git diff --name-status <base>...HEAD
git diff <base>...HEAD
git log --oneline <base>..HEAD
```

可选辅助脚本：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File ".claude\tools\pr\review_local_pr.ps1" -Base main
```

边界：

- 只读扫描当前分支 diff。
- 报告输出到对话；如用户明确需要，可写到 ignored `.tmp/pr-review/<branch>.md`。
- 不运行 `gh pr review --submit`。
- 不运行 `gh pr merge`。
- 不运行 `git push`。
- 不提交。
- 不改代码。
