---
name: frontend-design-project-bridge
description: 用于本仓前端 UI / 视觉 / 交互任务；只桥接全局 frontend-design，不重复全局设计规则，不覆盖项目 AGENTS。
---

# frontend-design-project-bridge

## trigger

- 前端 UI、视觉风格、交互、响应式布局、截图验收或浏览器验证任务。

## scope

- 桥接全局 `frontend-design`。
- 默认使用简体中文输出计划、进展、风险、验证和总结；路径、命令、API 和错误原文保持原文。
- 不重复全局设计规则。
- 不覆盖 `AGENTS.md`、子项目规则或用户本轮限制。
- 前端代码任务仍必须触发 `karpathy-project-bridge`。

## forbidden actions

- 不新增 repo-local frontend 依赖或配置，除非用户明确要求。
- 不改业务布局范围外的代码。
- 不触碰未授权工作区。
- 不将截图或视觉检查替代构建、lint 或测试。

## verification expectation

- 修改后按 `docs/当前规则/30-验证与审查.md` 选择最小有效验证，或运行子项目已有验证命令。
- 需要浏览器验收时，使用任务级 Playwright 或现有预览方式，并报告无法运行的原因。
- 收尾必须列出验证结果和剩余视觉风险。
