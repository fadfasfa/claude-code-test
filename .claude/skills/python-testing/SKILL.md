---
name: python-testing
description: Add or update Python tests, run them, and provide verification evidence without enforcing strict TDD.
---

# python-testing

中文简介：本 skill 用于 Python 行为变更需要测试覆盖时。它约束最小有效测试和验证证据；不强制 strict TDD，也不负责 hook、MCP、worktree 或 branch 策略。

## 什么时候使用

Python behavior changes 需要覆盖或回归保护时使用。

## 必要行为

1. 为变更行为添加或更新最小有效测试覆盖。
2. 本地运行相关 test command。
3. 报告精确 test evidence 或 blocker。

## 规则

- 本阶段不强制 strict TDD。
- 重点是 regression protection 和 reproducible verification。
- 不引入 hooks、MCP dependencies、worktree logic 或 forced branch policy。
