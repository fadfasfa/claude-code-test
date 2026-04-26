# claudecode

`claudecode` is a multi-purpose local development repository. It contains several independent work areas plus repo-local Claude Code workflow rules.

## Entry Points

- `AGENTS.md`: agent rules, boundaries, git confirmation rules
- `CLAUDE.md`: Claude Code entry and read order
- `PROJECT.md`: repository map
- `work_area_registry.md`: business work-area registry
- `agent_tooling_baseline.md`: repo-local tooling baseline
- `docs/task-routing.md`: small / medium / large task routing
- `docs/safety-boundaries.md`: safety boundaries

## Operating Model

- Small tasks and clear bug fixes use a lightweight patch-and-verify flow.
- Medium tasks use a short plan and explicit verification.
- Large tasks are the path for requirement narrowing, decomposition, worktree isolation, TDD, subagent parallelism, and PR-style review.

This repo does not inherit `kb` workflow and does not modify global Claude Code / Codex / Superpowers / ECC layers.

## Before Business Edits

1. Check `git status --short --branch`.
2. Read `work_area_registry.md`.
3. Declare the `target_work_area`.
4. Use the nearest repo-native verification.

Do not use heavy workflow for trivial changes.
