# claudecode Agent Rules

`claudecode` is a multi-purpose development repository. It hosts several independent work areas plus repo-local Claude Code workflow rules. The repository root is a governance and routing surface, not the default place for business implementation.

## Rule Chain

For generic agent or Codex-style sessions, read these repo-local files before changing code or workflow files. Claude Code sessions start at `CLAUDE.md`, then follow this file; that is the same rule chain with a Claude-specific entrypoint.

1. `AGENTS.md`
2. `CLAUDE.md`
3. `PROJECT.md`
4. `work_area_registry.md`
5. `agent_tooling_baseline.md`
6. Relevant files under `docs/`

This repository does not inherit `kb` workflow. `kb` can be read only when the user explicitly asks for boundary comparison or pollution-risk checks. Do not modify `kb` from this repo workflow.

This repository also does not modify global Claude Code, Codex, Superpowers, ECC, CLI, VS plugin, Codex App, Codex Proxy, global hooks, or global skills unless the user starts a separate global-layer task.

## Task Routing

Use `docs/task-routing.md` as the detailed routing rule.

- Small tasks and clear bug fixes use the lightweight path: confirm target work area, inspect the narrow files, patch, run the closest useful verification, report.
- Medium tasks use a short plan plus explicit verification. TDD, worktree, subagents, and PR review are optional and should be justified by risk.
- Large tasks are the only default path for requirement narrowing, decomposition, worktree isolation, TDD, subagent parallel work, and PR-style review.
- Do not apply heavy workflow to trivial edits, docs-only cleanup, obvious typos, or one-file low-risk fixes.

## Write Boundaries

- Pick a `target_work_area` from `work_area_registry.md` before business implementation.
- If the target is unclear, stay read-only and list candidate work areas.
- Root files may be edited only for explicit repo governance, routing, safety, or documentation tasks.
- Do not cross-write between `run/`, `sm2-randomizer/`, `QuantProject/`, `heybox/`, `qm-run-demo/`, or other work areas unless the user explicitly scopes a cross-work-area task.
- Dirty worktrees must be grouped and understood before editing. Never use `reset`, `clean`, or `stash` to make the tree easier to handle.

## Git Rules

`git add` and `git commit` may proceed only when all of these are true:

- The user explicitly authorized them in the accepted plan.
- The diff range is clear.
- The diff contains only current-task files.
- The commit message is confirmed or the plan contains a precise template.
- The dirty tree has no unrelated user changes mixed into the commit range.

Always stop for human confirmation before `push`, PR creation, `merge`, `reset`, `clean`, `rebase`, `stash`, or `git worktree remove`.

## Continuous Execution

Use `docs/continuous-execution.md` for long-running task governance.

- The active-task ledger path is `.tmp/active-task/current.md`.
- The ledger is runtime state only. It is not a rules file, not learning, and never authorizes dangerous operations.
- A Stop hook may only remind when the ledger says the accepted plan is unfinished.
- StopFailure may only log failure context.
- Hooks must not auto-dispatch, auto-continue, or modify business files.

## Module Admission

Use `docs/module-admission.md` before adding repo-local workflow modules, hooks, tools, Playwright configuration, validation scripts, or skills beyond the current accepted scope.

Any module admission card must state what it solves, what it does not solve, trigger conditions, read paths, write paths, dependency/install behavior, browser behavior, git/worktree/global/kb impact, disable/delete path, minimal validation command, and why existing modules are insufficient.

## Frontend Capability

Playwright and `frontend-polish-lite` are claudecode-only, coding-only frontend validation tools. They do not enter global core, do not enter `kb`, do not write global hooks, and do not run for every task.

Use them only for frontend tasks involving UI interaction, page behavior, screenshots, visual regression, responsive layout, or obvious accessibility checks.

## ECC and Superpowers

ECC is not an active workflow source for this repository. Only claudecode-local ECC residue may be inventoried here; global ECC retirement is a separate task.

Superpowers is not enabled as a default SessionStart workflow in this repository. Superpowers/TDD can be considered only as an explicit, task-scoped coding route after module admission and user confirmation.
