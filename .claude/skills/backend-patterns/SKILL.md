<!-- adopted-into-repo-baseline -->
---
name: backend-patterns
description: Project-level backend guidance focused on existing service boundaries, data flow, and safe incremental change.
---

# backend-patterns

Use this skill for server, job, or API implementation work.

## Rules

- Follow existing module boundaries and service patterns before inventing new ones.
- Keep changes incremental and observable.
- Prefer explicit error handling and narrow interfaces.
- Verify with existing local scripts, tests, or smoke checks.
- No hooks, MCP requirements, worktree automation, or forced branch flow.
