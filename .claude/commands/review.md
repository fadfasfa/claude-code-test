---
description: 审查 GitHub PR；无参数且只有一个开放 PR 时直接审查，`all` 审查全部开放 PR
argument-hint: "[PR_NUMBER|all]"
allowed-tools: [Bash, Read, Grep, Glob]
---

执行项目 skill：`.claude/skills/review/SKILL.md`。

原始参数：`$ARGUMENTS`

审查时必须遵守 `docs/当前规则/10-工作区登记.md`、`docs/当前规则/20-Git与高危操作.md`、`docs/当前规则/30-验证与审查.md` 和 `docs/当前规则/40-Agent与Skill.md`。

按该 skill 的参数路由和只读审查规则执行。无参数且只有一个开放 PR 时直接审查该 PR；`all` / `--all` 审查全部开放 PR。
