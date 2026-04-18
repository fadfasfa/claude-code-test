<!-- adapted-from-upstream: C:\Users\apple\.agents\skills\frontend-patterns\SKILL.md -->
---
name: frontend-patterns
description: Project-level frontend guidance that favors existing UI patterns, accessibility, and verification evidence.
---

# frontend-patterns

Use this skill for browser-facing UI work in this repo.

## Rules

- Start from existing components, styles, and interaction patterns in the repo.
- Prefer clear state flow over clever abstractions.
- Preserve accessibility basics: semantics, focus, labels, keyboard behavior.
- Validate the changed UI with the narrowest available local checks.
- Do not introduce hooks, MCP dependencies, worktree logic, or forced branch policy.
