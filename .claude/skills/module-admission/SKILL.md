<!-- claudecode-repo-local -->
---
name: module-admission
description: Produce a required module admission card before adding claudecode workflow modules, hooks, tools, scripts, Playwright config, or new skills.
---

# module-admission

中文简介：本 skill 用于新增或扩展 repo-local workflow modules 前生成模块准入卡。它约束 hook、tool、script、Playwright config 和 new skills 的准入；不负责直接实现模块或授权危险操作。

## 什么时候使用

新增或扩展 repo-local workflow modules 前使用本 skill。

## 必填卡片

包含：

- Name
- Type
- Solves
- Does not solve
- Trigger conditions
- Reads
- Writes
- Installs dependencies
- Runs browser
- Affects git/worktree/global/kb
- Disable
- Delete
- Minimal validation command
- Why existing modules are insufficient
- Status

## 规则

- 卡片是提案，不是危险操作授权。
- 用户确认卡片前，不写 hooks、tools、Playwright config、validation scripts 或 new dependencies。
- 不修改全局 Claude Code、Codex、Superpowers、ECC、CLI、VS plugin、Codex App、Codex Proxy 或 `kb`。
- 优先 repo-local skills，不优先新增 slash commands。
- 模块必须 task-scoped，并且禁用 / 删除路径清晰。

## 输出

先返回填写完成的 card。如果用户已经批准实现，在实现报告或相关 doc 中保留这张 card。
