---
name: repo-local-pr-review
description: 用于本仓提交或 PR 前的本地 diff 审查；生成风险、未验证点和建议验证命令；不调用云端 PR，不执行发布动作。
---

# repo-local-pr-review

## trigger

- 用户要求 review、local review、PR 前检查或提交前自审。
- 准备发布前需要确认 diff 范围和风险。

## scope

- 读取 `git status`、`git diff --stat`、`git diff --name-status`。
- 使用 `scripts/workflow/local-review.ps1` 生成本地摘要。
- 检查是否触碰 `run/**`、敏感文件名或未授权工作区。

## forbidden actions

- 不调用云端 PR。
- 不执行 git add、commit、push、merge、rebase。
- 不修改业务文件。
- 不读取凭据、token、cookie、auth 或 proxy secret。

## verification expectation

- 输出风险点、未验证点和建议验证命令。
- 如果 diff 触碰受保护资产，必须明确标出。
- 不把本地 review 结果当作测试通过。
