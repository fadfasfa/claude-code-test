# Continuous Execution

The goal is to keep Claude Code moving through an accepted plan until a clear endpoint, while preserving safety boundaries.

## Runtime Ledger

Path:

```text
.tmp/active-task/current.md
```

The ledger is ignored runtime state. It is not a rules layer, not learning, not a commit artifact, and not an authorization token.

Recommended ledger fields:

```markdown
# Active Task

- User goal:
- Accepted plan:
- Current phase:
- Completed:
- Next safe step:
- Blockers:
- Files in scope:
- Files explicitly out of scope:
- Verification plan:
- Dangerous operations still requiring confirmation:
- Resume notes:
```

## Continue Automatically

Continue without asking again when:

- the user has accepted a plan
- the next step is inside the accepted scope
- the write paths are current-task files inside `C:\Users\apple\claudecode`
- no install/uninstall/upgrade is needed
- no global or `kb` write is needed
- no dangerous git operation is needed
- dirty-tree ownership is clear
- verification commands are read/status/test/smoke commands within scope

## Stop And Ask

Stop for user confirmation when:

- scope changes
- target work area is unclear
- dirty tree includes unrelated user changes in the same files
- dependency install/uninstall/upgrade is needed
- global layer or `kb` would need modification
- Playwright config/script/hook/tool changes are needed without a module card
- git operation is `push`, PR, `merge`, `reset`, `clean`, `rebase`, `stash`, or `worktree remove`
- `git add` / `git commit` lacks explicit plan authorization, clear diff range, or confirmed message

## Blocker Report

Generate a blocker report when progress cannot continue safely.

Include:

- blocked step
- exact reason
- files/commands involved
- current repository status if relevant
- safe options
- what confirmation is needed

## Handoff Draft

Generate a handoff draft when:

- context is getting too long
- the task is paused by the user
- a long command or external process cannot finish in this turn
- work must move to another session

The handoff should include goal, accepted plan, completed items, current files touched, verification already run, next step, and remaining confirmations.

## Resume After Interruption

On resume:

1. Read `AGENTS.md`, `CLAUDE.md`, and this document.
2. Read `.tmp/active-task/current.md` if it exists.
3. Run `git status --short --branch`.
4. Compare ledger scope against the current dirty tree.
5. Continue only if the next step is still safe and in scope.
6. Otherwise produce a blocker or handoff report.

## Stop Hook Policy

`stop-guard-lite` is only a candidate until explicitly approved.

If approved later:

- Stop hook may read `.tmp/active-task/current.md`.
- If the ledger says the plan is unfinished, it may remind the agent to continue, write a handoff, or explain a blocker.
- It must not auto-run commands.
- It must not auto-continue the session.
- It must not edit business files.
- StopFailure may only log failure context.

Dangerous operations always remain manual-confirmation actions.
