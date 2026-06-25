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
- 按 `docs/当前规则/30-验证与审查.md` 的收尾报告要求检查 `run/**`、`sm2-randomizer/**`、`sms-monitor/**`、`heybox/**`、`subtitle_extractor/**`、`QuantProject/**` 和 `qm-run-demo`。
- 如涉及工作区、Git 高危操作或 skill 入口，必须引用 `docs/当前规则/10-工作区登记.md`、`docs/当前规则/20-Git与高危操作.md`、`docs/当前规则/30-验证与审查.md` 和 `docs/当前规则/40-Agent与Skill.md`。
- 默认使用简体中文输出修改文件、验证结果、风险和剩余未验证点。

## forbidden actions

- 不凭推测宣布完成。
- 不默认执行 git add、commit、push、clean、reset。
- 不触碰凭据、token、auth、cookie、API key、proxy 或全局配置。

## verification expectation

- 没有实际验证时，不得宣称完成。
- 验证失败时，必须明确失败点和下一步。
- 如果无法验证，必须说明具体原因。
- 如果涉及备份，必须报告备份是否成功；备份失败即停止。
