---
name: frontend-patterns
description: Project-level frontend guidance that favors existing UI patterns, accessibility, and verification evidence.
---

# frontend-patterns

中文简介：本 skill 用于本仓 browser-facing UI 工作。它约束前端模式、可访问性和验证证据；不负责引入 hook、MCP、worktree 逻辑或强制 branch 策略。

## 什么时候使用

处理本仓前端 UI、组件、页面行为或浏览器交互时使用。

## 文档和注释

- 新 frontend source files 必须包含简短中文文件级说明，说明它负责的 surface、component group 或 browser behavior。
- 关键 components、hooks、state adapters 和 script entrypoints 在 props、副作用、可访问性行为或数据流不明显时，应补 JSDoc 或短 docstring。
- Tooling、hook 和 workflow UI scripts 必须说明安全边界，不得暗示它们可以修改 settings 或 worktree，除非设计就是如此。
- 避免显而易见的注释；注释应解释非显然 UI 约束、accessibility invariants 和 workflow safety rules。

## 规则

- 从仓库已有 components、styles 和 interaction patterns 出发。
- 优先清晰状态流，而不是炫技抽象。
- 保留可访问性基础：semantics、focus、labels、keyboard behavior。
- 用最窄可用的本地检查验证 UI 变更。
- 不引入 hooks、MCP dependencies、worktree logic 或 forced branch policy。
