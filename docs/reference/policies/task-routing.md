# 任务路由参考

本文件只解释路由概念；权威边界见 `AGENTS.md`，官方 Superpowers plugin 为唯一 Superpowers 来源。

## S/M/L

- `S`：只改变文字表达或非行为性注释，不改变运行结果。
- `M`：有限范围行为变化，需要最小设计、验证和清晰收尾。
- `L`：workflow、agent 规则、plugin、hook、proxy、权限链路、仓库结构、关键数据/策略/回测或跨模块修改，走完整治理和验证流程。

任何行为性改动最低为 `M`；高风险配置、hook、proxy 或规则改动不得归为 `S`。

## 升级判断

旧 CC-CX 工作流中的“升级判断”已废弃，不再作为路由、权限升级或 Git 授权来源。需要判断任务是否从 `S` 升到 `M/L` 时，只使用本文件的路由定义、`AGENTS.md` 和任务上下文。

## 目标工作区

业务修改前，先从 `docs/workflows/work_area_registry.md` 选择 `target_work_area`。目标不清时保持只读，列出候选工作区，只有无法安全假设时才询问。

## Subagent

Subagent 是可选辅助能力，不参与权限升级或 Git 授权扩散。只有用户授权、任务边界清楚、并行不会制造协调风险时才使用；review subagent 不得写文件、提交、push、merge 或修改配置。
