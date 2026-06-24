---
description: 审查全部开放 GitHub PR；等价于 `/review all`
argument-hint: ""
allowed-tools: [Bash, Read, Grep, Glob]
---

执行项目 skill：`.claude/skills/review/SKILL.md`。

审查时必须遵守 `docs/当前规则/10-工作区登记.md`、`docs/当前规则/20-Git与高危操作.md`、`docs/当前规则/30-验证与审查.md` 和 `docs/当前规则/40-Agent与Skill.md`。

按 `all` 参数执行，只读审查当前仓库全部开放 PR。等价于 `/review all` / `/review --all`。
