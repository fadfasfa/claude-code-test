---
name: karpathy-guardrail
description: Codex 项目级 Karpathy guardrail；用于非琐碎代码修改、架构/实现/debugging 计划、数据管线方案和方案输出防范围漂移。
---

# karpathy-guardrail

## trigger

- non-trivial code change
- architecture or implementation plan
- debugging plan
- data pipeline plan

## checklist

- 默认使用简体中文输出计划、进展、风险、验证和总结；生成计划文档时正文必须为简体中文，英文计划视为不合格。
- 软边界合同：方案或实现前先列用户目标、本轮交付物、非目标和预期验证。
- 任务理解：先复述目标、非目标、受影响文件和预期验证。
- 假设：列出实现依赖的关键前提，能从仓库验证的先验证。
- 不确定点：只在无法从本地事实源确认时提问。
- 方案收口自检：本轮每个实施项必须能对应一个本轮交付物；对应不上的调度、fallback、schema、API、兼容层或额外测试矩阵等只放“后续可选项”。
- 最小修改：一个补丁只解决一个清晰问题。
- 不顺手重构：不扩大范围，不重排无关代码，不改无关风格。
- 非强硬口径：不按类别硬禁额外工程化内容；若它直接阻塞本轮交付物，可进入本轮并说明对应关系。
- 验证目标：修改前明确最小有效验证，修改后执行并记录结果。
- 风险退出条件：遇到安全边界、凭据、破坏性操作或工具链异常时停止并报告 blocker。

## boundaries

- 不覆盖 `AGENTS.md`、`docs/当前规则/10-工作区登记.md`、`docs/当前规则/20-Git与高危操作.md`、`docs/当前规则/30-验证与审查.md` 或用户本轮限制。
- 不读取或修改 auth、token、cookie、API key、proxy secret。
- 不要求 Codex 依赖 Claude Code，也不要求 Claude Code 依赖 Codex。
- 没有用户当前轮显性点名或命令时，不调用、委派、审查或触发 OpenAI Codex plugin / Codex / CX。
- 旧 CC-CX 执行脚本如出现只作为 legacy/compat 线索，不作为主流程要求。
