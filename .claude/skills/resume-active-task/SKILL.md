<!-- claudecode-repo-local -->
---
name: resume-active-task
description: Resume an interrupted claudecode task from the ignored active-task ledger and current git state.
---

# resume-active-task

中文简介：本 skill 用于从 ignored active-task ledger 和当前 git state 恢复中断任务。它负责对账范围、判断下一步是否安全；不负责绕过安全边界、批准危险 git、修改 global/kb 或安装依赖。

## 什么时候使用

用户要求 resume、continue、recover 或接着一个已接受的多步任务继续时使用。

## 必要步骤

1. 读取 `AGENTS.md`、`CLAUDE.md` 和 `docs/continuous-execution.md`。
2. 如果 `.tmp/active-task/current.md` 存在，读取它。
3. 运行 `git status --short --branch`。
4. 对照 ledger scope 和当前 dirty tree。
5. 只有下一步仍在已接受计划和安全边界内时才继续。
6. 如果继续不安全，输出 blocker report 或 handoff draft。

## 规则

- ledger 只是 runtime state。
- ledger 不是 rules source、learning file 或 permission grant。
- 永远不要用 ledger 授权 `push`、PR、`merge`、`reset`、`clean`、`rebase`、`stash` 或 `git worktree remove`。
- `git add` / `git commit` 仍需要明确计划授权、清晰 diff scope、仅当前任务文件和已确认 message。
- 不修改 global config 或 `kb`。
- 不安装依赖。

## 输出

报告：

- current phase
- completed items
- next safe action
- blocker，如有
- verification still needed
