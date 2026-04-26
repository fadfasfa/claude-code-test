<!-- adopted-into-repo-baseline -->
---
name: backend-patterns
description: Project-level backend guidance focused on existing service boundaries, data flow, and safe incremental change.
---

# backend-patterns

中文简介：本 skill 用于本仓 server、job 或 API 实现工作。它约束后端边界、数据流、注释和验证；不负责引入 hook、MCP、worktree 自动化或强制 branch 流程。

## 什么时候使用

处理本仓 backend、server、job 或 API implementation work 时使用。

## 文档和注释

- 新 backend source files 必须包含简短中文文件头或 module docstring，说明文件职责和 service/runtime 边界。
- 关键函数、类、API handlers、jobs 和 script entrypoints 在 contract、副作用、数据所有权或失败模式不明显时，应补 docstring。
- Workflow-control、hook 和 tooling code 必须说明安全边界，特别是是否会写文件、触碰 settings 或影响 worktree。
- 避免显而易见的注释；注释应解释约束、invariants 和安全决策。

## 规则

- 先遵循已有 module boundaries 和 service patterns，再考虑新增抽象。
- 修改保持增量、可观察。
- 优先显式错误处理和窄接口。
- 使用已有 local scripts、tests 或 smoke checks 验证。
- 不新增 hooks、MCP requirements、worktree automation 或 forced branch flow。
