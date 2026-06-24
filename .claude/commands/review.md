---
description: 审查 GitHub PR；无参数且只有一个开放 PR 时直接审查，`all` 审查全部开放 PR
argument-hint: "[PR_NUMBER|all]"
allowed-tools: [Bash, Read, Grep, Glob]
---

执行项目 skill：`.claude/skills/review/SKILL.md`。

原始参数：`$ARGUMENTS`

按该 skill 的参数路由和只读审查规则执行。无参数且只有一个开放 PR 时直接审查该 PR；`all` / `--all` 审查全部开放 PR。
