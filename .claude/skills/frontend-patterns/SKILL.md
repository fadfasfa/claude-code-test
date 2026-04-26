<!-- adopted-into-repo-baseline -->
---
name: frontend-patterns
description: Project-level frontend guidance that favors existing UI patterns, accessibility, and verification evidence.
---

# frontend-patterns

Use this skill for browser-facing UI work in this repo.

## Documentation and comments

- New frontend source files must include a concise 中文（Chinese） file-level description of the surface, component group, or browser behavior they own.
- Add JSDoc or short docstrings to key components, hooks, state adapters, and script entrypoints when props, side effects, accessibility behavior, or data flow are not obvious.
- Tooling, hook, and workflow UI scripts must document their safety boundary and avoid implying they can change settings or worktrees unless explicitly designed to do so.
- Avoid obvious comments; use comments for non-obvious UI constraints, accessibility invariants, and workflow safety rules.

## Rules

- Start from existing components, styles, and interaction patterns in the repo.
- Prefer clear state flow over clever abstractions.
- Preserve accessibility basics: semantics, focus, labels, keyboard behavior.
- Validate the changed UI with the narrowest available local checks.
- Do not introduce hooks, MCP dependencies, worktree logic, or forced branch policy.
