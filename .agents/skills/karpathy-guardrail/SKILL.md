---
name: karpathy-guardrail
description: Codex 项目级 Karpathy guardrail；用于非琐碎代码修改、架构计划和 debugging 计划。
---

# karpathy-guardrail

## trigger

- non-trivial code change
- architecture plan
- debugging plan

## checklist

- 任务理解：先复述目标、非目标、受影响文件和预期验证。
- 假设：列出实现依赖的关键前提，能从仓库验证的先验证。
- 不确定点：只在无法从本地事实源确认时提问。
- 最小修改：一个补丁只解决一个清晰问题。
- 不顺手重构：不扩大范围，不重排无关代码，不改无关风格。
- 验证目标：修改前明确最小有效验证，修改后执行并记录结果。
- 风险退出条件：遇到安全边界、凭据、破坏性操作或工具链异常时停止并报告 blocker。

## boundaries

- 不覆盖 `AGENTS.md`、`work_area_registry.md` 或 workflow scripts。
- 不读取或修改 auth、token、cookie、API key、proxy secret。
- 不要求 Codex 依赖 Codex，也不要求 Codex 依赖 Codex。
- 没有用户当前轮显性点名或命令时，不调用、委派、审查或触发 OpenAI Codex plugin / Codex / CX。
- 旧 CC-CX 执行脚本如出现只作为 legacy/compat 线索，不作为主流程要求。
