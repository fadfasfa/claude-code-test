# Git Policy

## Default

- 默认允许 `git status`、`git diff`、`git log` 等只读命令。
- 不默认执行 `git add`、`commit`、`push`、`clean`、`reset`、`rebase`、`stash`、`switch`、`branch` 或 worktree 写入。
- Guard 对未授权 `git add`、`git commit`、`git push` 直接 deny；授权可通过 `.state/cc-work/cc-cx-state.json` 的 `git.add`、`git.commit`、`git.push` 表达。
- 不覆盖、不回滚、不清理与当前任务无关的脏树改动。

## Explicit Authorization

只有用户当前轮明确要求对应动作，才执行 Git 写入，包括 stage、commit、push、branch、checkout、switch、merge、rebase、reset、clean、stash、tag、cherry-pick、revert、worktree、remote 或 PR 操作。

授权一级不隐含下一级：`git add` 不隐含 commit，commit 不隐含 push，push 不隐含 PR 或 merge。push 永远必须单独授权。

## Before Git Writes

执行前必须确认：

- 当前分支和 `git status --short`。
- staged 清单只包含本轮授权范围。
- 禁止路径、受保护资产和无关业务脏改没有进入 staged。
- 验证命令和结果可复述；无法验证时说明原因。

选择性暂存优先使用明确路径，避免 `git add .`。创建分支默认用 `codex/` 前缀，不强制覆盖既有分支。

## Publish Boundary

push、PR、merge、amend、tag 和 release 类动作只在用户明确授权下执行。merge 不进入默认自动路径。
