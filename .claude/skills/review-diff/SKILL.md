<!-- claudecode-repo-local -->
---
name: review-diff
description: Review a claudecode diff for behavior risk, boundary violations, verification gaps, and commit readiness.
---

# review-diff

中文简介：本 skill 用于审查 claudecode diff 的行为风险、边界风险、验证缺口和提交就绪度。它负责 review report；不负责创建 PR、push、merge、rebase、stash、reset、clean 或删除 worktree。

## 什么时候使用

用户要求 review、提交前，或 medium/large patch 之后使用。

## 输入

检查：

- `git status --short --branch`
- `git diff --stat`
- `git diff -- <current-task-files>`
- 变更区域相关 docs/tests
- verification output

## Review 顺序

1. 边界违规：global、`kb`、错误工作区、hooks/settings、依赖安装、dirty-tree 混入。
2. 行为风险：regression、缺少 edge cases、破坏 contract。
3. 验证缺口：缺少或较弱的 command evidence。
4. 提交就绪度：diff scope、无关文件、message availability。

## 规则

- findings 优先，按严重程度排序。
- 文件 / 行引用要具体。
- 不创建 PR、不 push、不 merge、不 rebase、不 stash、不 reset、不 clean、不 remove worktree。
- 除非用户已经在计划中授权且 diff scope 干净，否则不 stage、不 commit。

## 输出

如果存在问题，先列 findings。如果没有发现问题，明确说明，并指出剩余测试或 review 风险。
