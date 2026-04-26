<!-- locally-authored-minimal -->
---
name: requesting-code-review
description: Prepare concise review-ready context focused on changed behavior, risks, and verification evidence.
---

# requesting-code-review

中文简介：本 skill 用于把当前工作整理成可审查上下文。它负责说明行为变化、风险和验证证据；不负责发起强制 branch/worktree 流程，也不制造不存在的步骤。

## 什么时候使用

需要把变更交给另一个 human 或 agent review 时使用。

## 包含

- 行为或 contract 改了什么。
- 仍存在哪些风险或 regression。
- 运行了什么验证，哪些通过。
- 哪些 open questions 会改变 review 结论。

## 排除

- 空泛表扬。
- 编造的 workflow steps。
- 强制 branch 或 worktree policy。
