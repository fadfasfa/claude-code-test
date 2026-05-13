# Git Policy

## 默认只读

- 默认允许 `git status`、`git diff`、`git log` 等只读命令。
- 不默认执行 `git add`、`commit`、`push`、`clean`、`reset`、`rebase`、`stash`。

## 授权要求

- commit 需要用户明确授权。
- push、PR、merge 需要用户明确授权，并在执行前二次确认。
- 不覆盖、不回滚、不清理与当前任务无关的脏树改动。

## 发布前检查

- 工作区选择明确。
- diff 只包含授权范围。
- 验证结果可复述。
- 未验证点和残余风险已写明。
