<!-- adopted-into-repo-baseline -->
---
name: backend-patterns
description: Project-level backend guidance focused on existing service boundaries, data flow, and safe incremental change.
---

# backend-patterns

Use this skill for server, job, or API implementation work.

## Documentation and comments

- New backend source files must include a concise 中文（Chinese） file header or module docstring that states the file purpose and service/runtime boundary.
- Key functions, classes, API handlers, jobs, and script entrypoints should carry docstrings when contracts, side effects, data ownership, or failure modes are non-obvious.
- Workflow-control, hook, and tooling code must state its safety boundary, especially whether it may write files, touch settings, or affect worktrees.
- Avoid obvious comments; prefer comments that explain constraints, invariants, and safety decisions.

## Rules

- Follow existing module boundaries and service patterns before inventing new ones.
- Keep changes incremental and observable.
- Prefer explicit error handling and narrow interfaces.
- Verify with existing local scripts, tests, or smoke checks.
- No hooks, MCP requirements, worktree automation, or forced branch flow.
