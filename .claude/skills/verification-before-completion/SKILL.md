<!-- locally-authored-minimal -->
---
name: verification-before-completion
description: Require concrete verification evidence before claiming a task is complete.
---

# verification-before-completion

Use this skill before marking any implementation task as done.

## Required behavior

1. For behavior changes, bug fixes, or refactors that affect runtime paths, first locate the nearest existing test; when a small regression test is feasible, add or update it before changing implementation.
2. For docs, comments, read-only reviews, or pure organization-only changes, new tests are optional, but the final report must say why no test was added.
3. Choose verification in this order: nearest test -> changed module test -> smoke / fast verification -> full test suite.
4. Until the test entrypoint is confirmed, use read-only discovery only; do not install dependencies, initialize a new test framework, download from the network, or change global config.
5. Run the narrowest relevant verification commands for the change and inspect the output rather than assuming success.
6. In the final summary, report the exact commands run, test scope, result, and failure summary; if verification cannot run, say exactly why.
7. Distinguish pre-existing failures from failures introduced by the current change whenever output or a baseline comparison allows it.

## Read fallback protocol

- If the Read tool fails twice in a row for the same file, stop repeating the same Read call.
- Fall back to a read-only route such as PowerShell `Get-Content`, a Python read-only script, `git show` / `git diff`, or smaller ranged reads.
- If fallback reading still fails, report the blocker and do not guess at file contents.

## Rules

- Never claim completion without evidence.
- Prefer repo-native commands and existing test scripts.
- Do not add hooks, MCP requirements, worktrees, forced branch logic, or TDD enforcement.
- Do not clean, reset, stash, push, change hooks, change global Claude Code settings, or handle locked worktrees for the sake of testing.
