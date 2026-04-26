# Task Routing

This document decides how much workflow a `claudecode` task needs.

## Default Classifier

| Task class | Signals | Flow |
| :--- | :--- | :--- |
| Small | One narrow file or doc, obvious bug, typo, small config/doc clarification, no shared contract change | Confirm target, inspect narrow context, patch, run closest useful verification, report |
| Medium | Multiple files in one work area, behavior change with local risk, unclear test entrypoint, moderate UI/API change | Short plan, identify verification, patch in small steps, verify, report |
| Large | Cross-work-area change, shared data contract, major refactor, migration, release/PR work, high rollback cost, unclear requirements | Requirements narrowing, decomposition, checkpoints, optional worktree, optional TDD, optional subagents, PR-style review |

For large tasks, a lightweight brainstorm / option comparison may happen before the detailed plan to narrow direction; brainstorm itself does not replace the acceptance plan, task split, or verification.

If the route is unclear, choose the lighter route first and escalate only when evidence shows the task needs it.

## When A Plan Is Needed

No formal plan is needed for:

- trivial text edits
- one-line fixes
- narrow docs cleanup
- read-only inventory
- obvious local bug fixes where the command path is clear

A short plan is needed for:

- medium tasks
- multiple files in one work area
- any task touching workflow docs, hooks, skills, tools, or settings
- frontend changes requiring visual validation
- data contract changes

A detailed plan is needed for:

- large tasks
- worktree use
- TDD route
- subagent parallel implementation
- PR/release route
- risky cleanup or migration

## When TDD Is Needed

Use TDD or test-first only when it materially controls risk:

- bug has a clear regression shape
- shared API/data contract may regress
- large refactor with existing tests
- parser, serializer, routing, or state-machine behavior is changing

Do not force TDD for docs-only tasks, exploratory reads, tiny UI copy edits, or tasks with no practical local test harness.

## When Subagents Are Needed

Subagents are optional and only useful when the work can be bounded.

Use them for:

- read-only codebase exploration
- test discovery
- failure attribution
- review
- disjoint implementation slices when ownership is explicit

Do not use them for:

- urgent blocking work on the critical path
- same-file concurrent edits
- settings, hooks, worktree policy, or shared data contracts unless explicitly planned
- tasks where coordination overhead is larger than the work

## When Worktree Is Needed

Use `docs/git-worktree-policy.md`.

Worktree is usually needed only when:

- the current tree has unrelated changes that block safe edits
- the task is large enough to need isolation
- the user explicitly asks for isolated execution
- parallel implementation needs separate branches or trees

Worktree is not needed for small docs/rules edits, local bug fixes, or read-only inventory.

Creating a worktree without `-DryRun` requires explicit user instruction. Removing a worktree always requires human confirmation.

## When PR Review Is Needed

PR-style review is appropriate for:

- large task completion
- behavior changes with high blast radius
- cross-work-area changes
- changes that will be pushed or reviewed externally
- workflow/safety changes that affect future agent behavior

PR review is not required for trivial docs edits, small local fixes, or read-only reports.

## Verification Floor

Every non-read-only task needs a completion report with:

- changed files
- exact verification command or reason no command was relevant
- result
- remaining risk

Use `verification-before-completion` for implementation tasks.
