# Git Policy

## Default

- 默认允许 `git status`、`git diff`、`git log` 等只读命令。
- 每次任务开始先运行 `git status --short`。
- 验证通过后，按本轮授权自动 commit。
- commit 前只能暂存本轮修改文件，禁止 `git add .`。
- 不默认执行 `push`、`clean`、`reset`、`rebase`、`stash`、`switch`、`branch` 或 worktree 写入。
- 不覆盖、不回滚、不清理与当前任务无关的脏树改动。

## Explicit Authorization

只有用户当前轮明确要求对应动作，才执行 push、branch、checkout、switch、merge、rebase、reset、clean、stash、tag、cherry-pick、revert、worktree、remote 或 PR 操作。

commit 授权不隐含 push，push 不隐含 PR 或 merge。push 永远必须单独授权。

## Before Git Writes

执行前必须确认：

- 当前分支和 `git status --short`。
- staged 清单只包含本轮授权范围。
- 高危资产和无关业务脏改没有进入 staged。
- 验证命令和结果可复述；无法验证时说明原因。

选择性暂存优先使用明确路径，避免 `git add .`。创建分支默认用 `codex/` 前缀，不强制覆盖既有分支。

## Publish Boundary

push、PR、merge、amend、tag 和 release 类动作只在用户明确授权下执行。merge 不进入默认自动路径。
