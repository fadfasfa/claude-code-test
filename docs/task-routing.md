# 任务路由

本文件决定 `claudecode` 任务需要多重的工作流。

## 默认分类器

| 任务级别 | 信号 | 流程 |
| :--- | :--- | :--- |
| Small | 单个窄文件或文档、明显 bug、错字、小配置 / 文档澄清、不改变共享契约 | 确认目标，读取窄范围上下文，修改，运行最近的有效验证，报告 |
| Medium | 同一工作区内多文件、局部风险行为变更、测试入口不清、中等 UI/API 变更 | 简短计划，识别验证方式，小步修改，验证，报告 |
| Large | 跨工作区变更、共享数据契约、重大重构、迁移、release/PR 工作、高回滚成本、需求不清 | 需求收敛、任务拆分、checkpoint、可选 worktree、可选 TDD、可选 subagents、PR-style review |

对大任务，进入详细计划前可先做轻量 brainstorm / 方案比较，用于收敛方向；brainstorm 本身不得替代验收计划、任务拆分或验证。

如果路由不清，先选择较轻路径；只有证据表明任务需要时才升级。

## 什么时候需要计划

以下情况不需要正式计划：

- 琐碎文本编辑
- 一行修复
- 窄范围 docs cleanup
- 只读盘点
- 命令路径清晰的明显本地 bug 修复

以下情况需要简短计划：

- medium tasks
- 同一工作区内多文件
- 任何触碰 workflow docs、hooks、skills、tools 或 settings 的任务
- 需要视觉验证的前端变更
- 数据契约变更

以下情况需要详细计划：

- large tasks
- worktree use
- TDD route
- subagent parallel implementation
- PR/release route
- 高风险 cleanup 或 migration

## 什么时候需要 TDD

只有 TDD 或 test-first 能实际控制风险时才使用：

- bug 具有清晰 regression 形状
- shared API/data contract 可能回归
- 具有现有测试的大型重构
- parser、serializer、routing 或 state-machine 行为发生变化

不要为 docs-only 任务、探索性读取、微小 UI 文案修改或没有实用本地测试框架的任务强制 TDD。

## 什么时候需要 Subagents

Subagents 是可选项，只在工作能被清晰界定时有用。

适合用于：

- 只读代码库探索
- test discovery
- failure attribution
- review
- 文件所有权明确、互不重叠的实现切片

不适合用于：

- critical path 上的紧急阻塞工作
- 同一文件并发编辑
- settings、hooks、worktree policy 或 shared data contracts，除非明确计划
- 协调成本高于工作本身的任务

## 什么时候需要 Worktree

见 `docs/git-worktree-policy.md`。

通常只有以下情况需要 worktree：

- 当前 tree 有无关改动，阻碍安全编辑
- 任务大到需要隔离
- 用户明确要求隔离执行
- 并行实现需要单独 branch 或 tree

小型 docs/rules 编辑、本地 bug 修复或只读盘点不需要 worktree。

不带 `-DryRun` 创建 worktree 前，必须有明确用户指令。删除 worktree 永远需要人工确认。

## 什么时候需要 PR Review

PR-style review 适用于：

- large task completion
- blast radius 高的行为变更
- 跨工作区变更
- 需要 push 或外部 review 的变更
- 会影响未来 agent 行为的 workflow/safety 变更

琐碎 docs 编辑、小型本地修复或只读报告不需要 PR review。

## 验证底线

任何非只读任务的完成报告都需要包含：

- changed files
- 精确验证命令，或说明为什么没有相关命令
- result
- remaining risk

实现任务使用 `verification-before-completion`。
