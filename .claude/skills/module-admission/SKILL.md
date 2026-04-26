<!-- claudecode-repo-local -->
---
name: module-admission
description: Produce a required module admission card before adding claudecode workflow modules, hooks, tools, scripts, Playwright config, or new skills.
---

# module-admission

Use this skill before adding or expanding repo-local workflow modules.

## Required Card

Include:

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

## Rules

- A card is a proposal, not authorization for dangerous operations.
- Do not write hooks, tools, Playwright config, validation scripts, or new dependencies until the user confirms the card.
- Do not modify global Claude Code, Codex, Superpowers, ECC, CLI, VS plugin, Codex App, Codex Proxy, or `kb`.
- Prefer repo-local skills over slash commands.
- Keep modules task-scoped and disable/delete paths clear.

## Output

Return the filled card first. If the user already approved implementation, keep the card in the implementation report or relevant doc.
