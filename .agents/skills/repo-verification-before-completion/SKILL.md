---
name: repo-verification-before-completion
description: 在声明任务完成前使用；必须给出实际证据、验证命令、验证结果和剩余风险；不用于规划、实现、记忆、PR 发布或 worktree 管理。
---

# repo-verification-before-completion

## trigger

准备声明任务完成前使用，尤其是：

- 修改了文件。
- 清理了目录。
- 调整了规则、脚本、skill 或工作流。
- 改动涉及权限、配置或验证边界。

## scope

- 汇总修改文件。
- 汇总验证命令和结果。
- 说明禁止路径是否被触碰。
- 说明未验证部分和剩余风险。

## forbidden actions

- 不凭推测宣布完成。
- 不默认执行 git add、commit、push、clean、reset。
- 不触碰凭据、token、auth、cookie、API key、proxy 或全局配置。

## verification expectation

- 没有实际验证时，不得宣称完成。
- 验证失败时，必须明确失败点和下一步。
- 如果无法验证，必须说明具体原因。
- 如果涉及备份，必须报告备份是否成功；备份失败即停止。
