# Git Policy

## Default

- 默认允许 `git status`、`git diff`、`git log` 等只读命令。
- 每次任务开始先运行 `git status --short`。
- git 写操作必须有当前轮授权或已批准计划；授权已经明确时，agent 直接执行并验证，不重复要求业务层确认。
- 验证通过后报告 diff、验证结果和剩余风险；只有当前轮明确授权时才 commit。
- commit 前只能暂存本轮修改文件，禁止 `git add .`。
- 未获用户明确授权时，不主动执行 `push`、PR、merge、discard、`clean`、`reset`、`rebase`、`stash`、`switch`、`branch` 或 worktree 写入。
- 不覆盖、不回滚、不清理与当前任务无关的脏树改动。

## Explicit Authorization

只有用户当前轮明确要求对应动作，或当前轮批准的任务计划已经包含该动作，才执行 commit、push、branch、checkout、switch、merge、rebase、reset、clean、restore、stash、tag、cherry-pick、revert、worktree、remote 或 PR 操作。

commit 授权不隐含 push，push 不隐含 PR 或 merge，discard 授权也不隐含其他清理动作。用户明确授权 push、PR、merge 或 discard 后，agent 必须按确认范围自行完整执行并验证结果，不得退回为“请你在本机终端执行命令”。

## Before Git Writes

执行前必须确认：

- 当前分支和 `git status --short`。
- staged 清单只包含本轮授权范围。
- 高危资产和无关业务脏改没有进入 staged。
- 验证命令和结果可复述；无法验证时说明原因。
- Git 处理方式未超出本轮明确授权或已批准计划范围。

选择性暂存优先使用明确路径，避免 `git add .`。创建分支默认用 `codex/` 前缀，不强制覆盖既有分支。

## Publish Boundary

push、PR、merge、amend、tag、release 和 discard 类动作只在用户明确授权下执行。授权后由 agent 完整执行、验证远端或本地状态，并在收尾报告结果；merge 不进入默认主动路径。

## Language And Format

commit message 与 PR 的中文格式、type 字典、分支中文别名行、merge/revert/第三方例外，统一遵循 `git-language-policy.md`，本文件不重复细则。本节仅声明：本仓所有由 agent 产生的 commit 与 PR 必须满足该策略；不满足时视为未完成本轮 git 写动作，须修正后再报告。
