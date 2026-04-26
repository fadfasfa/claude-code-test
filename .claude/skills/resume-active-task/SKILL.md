<!-- claudecode-repo-local -->
---
name: resume-active-task
description: Resume an interrupted claudecode task from the ignored active-task ledger and current git state.
---

# resume-active-task

Use this skill when the user asks to resume, continue, recover, or pick up a previously accepted multi-step task.

## Required Steps

1. Read `AGENTS.md`, `CLAUDE.md`, and `docs/continuous-execution.md`.
2. Read `.tmp/active-task/current.md` if it exists.
3. Run `git status --short --branch`.
4. Compare the ledger scope with the current dirty tree.
5. Continue only when the next step is inside the accepted plan and safe boundaries.
6. If continuation is unsafe, produce a blocker report or handoff draft.

## Rules

- The ledger is runtime state only.
- The ledger is not a rules source, learning file, or permission grant.
- Never use the ledger to authorize `push`, PR, `merge`, `reset`, `clean`, `rebase`, `stash`, or `git worktree remove`.
- `git add` / `git commit` still require explicit plan authorization, clear diff scope, current-task-only files, and confirmed message.
- Do not modify global config or `kb`.
- Do not install dependencies.

## Output

Report:

- current phase
- completed items
- next safe action
- blocker, if any
- verification still needed
