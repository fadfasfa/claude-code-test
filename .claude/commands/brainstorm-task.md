---
description: Clarify requirements, compare approaches, and list risks without editing files.
argument-hint: "<task or question>"
---

# /brainstorm-task

本命令只用于需求澄清、方案比较和风险列举。默认输出中文。

执行规则：

- 默认不改文件。
- 不创建分支。
- 不启动 worktree。
- 不提交、不 push、不创建 PR。
- 不把 brainstorm 当作验收计划或实现授权。
- 如需源码或文档上下文，不调用原生 `Read` 读取 text/code 文件；使用 `repo-explorer`、`Grep` / `Glob` / `Bash`。
- 最多提出 1 个必须用户确认的问题；非关键歧义直接说明假设后继续。

输出建议：

1. 当前理解
2. 关键假设
3. 可选方案
4. 风险与取舍
5. 建议下一步
