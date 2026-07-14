---
name: karpathy-guardrail
description: 项目级方案范围 guardrail；仅用于架构、实现、debugging 或数据管线方案防范围漂移，不承担编码实现纪律。
---

# karpathy-guardrail

## trigger

- architecture or implementation plan
- debugging plan
- data pipeline plan
- 需要把方案收敛到当前交付物的非琐碎设计讨论

## checklist

- 默认使用简体中文输出计划、风险和方案结论；生成计划文档时正文必须为简体中文。
- 先列用户目标、本轮交付物、非目标和预期验证。
- 能从仓库验证的关键前提先验证，只询问无法从本地事实源确认的高影响问题。
- 每个方案项必须对应一个本轮交付物；对应不上的调度、fallback、schema、API、兼容层或额外测试矩阵只放“后续可选项”。
- 不按类别硬禁额外工程化内容；若它直接阻塞本轮交付物，可进入本轮并说明对应关系。
- 遇到安全边界、凭据、破坏性操作或工具链异常时停止并报告 blocker。

## boundaries

- 本 skill 只约束方案范围，不替代或复制原生 `karpathy-guidelines`；进入编码实现后由原生 skill 约束小步修改和验证。
- 不覆盖 `AGENTS.md`、`docs/当前规则/10-工作区登记.md`、`docs/当前规则/20-Git与高危操作.md`、`docs/当前规则/30-验证与审查.md` 或用户本轮限制。
- 不读取或修改 auth、token、cookie、API key、proxy secret。
- 不要求 Codex 依赖 Claude Code，也不要求 Claude Code 依赖 Codex。
- 没有用户当前轮显性点名或命令时，不调用、委派、审查或触发 OpenAI Codex plugin / Codex / CX。
