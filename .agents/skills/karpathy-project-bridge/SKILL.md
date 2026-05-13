---
name: karpathy-project-bridge
description: 用于本仓所有非琐碎代码任务；桥接全局 karpathy-guidelines，要求先读现有代码、小步直接实现、修改后验证。
---

# karpathy-project-bridge

## trigger

- 任何非琐碎代码、脚本、配置或工作流实现任务。
- 前端代码任务也必须触发本 skill。

## scope

- 桥接全局 `karpathy-guidelines`。
- 要求先读现有代码和附近规则，再改文件。
- 优先小步、直接、可局部理解的代码。
- 避免顺手重构和过早抽象。

## forbidden actions

- 不覆盖 `AGENTS.md`、`work_area_registry.md` 或用户本轮限制。
- 不绕过验证。
- 不把未验证的实现宣称完成。
- 不触碰未授权业务工作区、凭据或受保护资产。

## verification expectation

- 修改后运行 `scripts/workflow/verify.ps1` 或更具体的子项目验证。
- 无法验证时说明具体 blocker。
- 收尾必须报告验证命令和结果。
