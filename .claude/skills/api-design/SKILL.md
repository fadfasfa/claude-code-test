<!-- adopted-into-repo-baseline -->
---
name: api-design
description: Lightweight API design guardrails for consistent naming, error handling, and compatibility.
---

# api-design

中文简介：本 skill 用于新增或修改 API contract 时控制命名、错误处理和兼容性。它约束接口形状和验证证据；不负责新增 hook、MCP-only 依赖或强制 Git 流程。

## 什么时候使用

当任务新增或修改 API contract 时使用本 skill。

## 规则

- 优先沿用项目已有 API 的命名和结构。
- 任何 breaking change 都必须显式说明。
- 请求和响应结构保持简单、可说明。
- 修改时附带 validation 和 verification 证据。
- 不附加 hooks、MCP-only 依赖或强制 Git workflow。
