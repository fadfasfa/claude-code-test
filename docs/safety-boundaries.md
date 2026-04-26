# Safety Boundaries

This document defines boundaries for `claudecode` repo-local work.

## Repository Scope

Allowed write scope in this workflow:

- `C:\Users\apple\claudecode\**`

Read-only boundary references when explicitly needed:

- `C:\Users\apple\kb\**`
- global Claude Code / Codex configuration paths

Forbidden from this repo workflow unless the user starts a separate task:

- `C:\Users\apple\.claude\**`
- `C:\Users\apple\.codex\**`
- `C:\Users\apple\kb\**`
- CLI install/uninstall/upgrade
- VS plugins
- Codex App
- Codex Proxy
- global Superpowers / ECC installation
- global skills
- global hooks
- global AGENTS / CLAUDE files

## Work-Area Boundary

Before business implementation, choose `target_work_area` from `work_area_registry.md`.

If the task is repo governance, use `repo-root-governance`.

If the target is unclear:

1. stay read-only
2. list candidate work areas
3. ask for direction only if a safe assumption is not possible

## Dirty Tree Boundary

The current repository may contain unrelated user changes. Do not revert, reset, stash, clean, or overwrite them.

Before committing or staging, group the diff by purpose and confirm the current task's exact file range.

## Git Confirmation Boundary

`git add` and `git commit` are allowed only inside an accepted plan with explicit user authorization and a clear diff range.

Always ask before:

- `git push`
- PR creation
- `git merge`
- `git reset`
- `git clean`
- `git rebase`
- `git stash`
- `git worktree remove`

## Hook Boundary

Repo hooks may only be:

- safety blocks
- naming guards
- lightweight reminders
- raw failure logging

Hooks must not:

- schedule tasks
- auto-continue execution
- modify business files
- install dependencies
- change global config
- become a complex workflow engine

`stop-guard-lite` remains a module-card candidate until the user explicitly approves writing a Stop hook.

## Frontend Boundary

Playwright and `frontend-polish-lite` are claudecode-only, coding-only tools.

They do not enter global core, do not enter `kb`, do not write global hooks, and do not run for every task.

Use them only for frontend UI interaction, page behavior, visual, responsive, or accessibility checks.

## ECC and Superpowers Boundary

ECC is not an active workflow source for this repo. Local ECC residue may be inventoried, but deletion or archiving requires a separate approved cleanup plan.

Superpowers is not default SessionStart. Superpowers/TDD can only be task-scoped after admission and confirmation.

## kb Boundary

`kb` is a knowledge-base workflow and must not inherit claudecode development flow.

Do not push TDD, worktree, PR, subagent-driven development, agent-first, commit-first, Playwright, frontend-polish-lite, ECC cleanup, or claudecode self-improvement flow into `kb`.
