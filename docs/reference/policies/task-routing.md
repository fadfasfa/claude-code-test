# 任务路由参考

本文件只解释路由概念；权威流程见 `.agents/skills/superpowers-project-bridge/SKILL.md`，不可违背摘要见 `AGENTS.md`。

## S/M/L

- `S`：只改变文字表达或非行为性注释，不改变运行结果。
- `M`：有限范围行为变化，必须触发 Superpowers，使用隔离 branch/worktree、计划和可重复验证。
- `L`：workflow、agent 规则、plugin、hook、proxy、权限链路、仓库结构、关键数据/策略/回测或跨模块修改，完整使用 Superpowers 流程。

任何行为性改动最低为 `M`；高风险配置、hook、proxy 或规则改动不得归为 `S`。

## 目标工作区

业务修改前，先从 `docs/workflows/work_area_registry.md` 选择 `target_work_area`。目标不清时保持只读，列出候选工作区，只有无法安全假设时才询问。

## Subagent

Subagent 是可选能力。只有用户授权、任务边界清楚、并行不会制造协调风险时才使用；review subagent 不得写文件、提交、push、merge 或修改配置。
