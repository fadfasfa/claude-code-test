<!-- claudecode-repo-local -->
---
name: review-diff
description: Review a claudecode diff for behavior risk, boundary violations, verification gaps, and commit readiness.
---

# review-diff

Use this skill when the user asks for a review, before a commit, or after a medium/large patch.

## Inputs

Inspect:

- `git status --short --branch`
- `git diff --stat`
- `git diff -- <current-task-files>`
- relevant docs/tests for the changed area
- verification output

## Review Order

1. Boundary violations: global, `kb`, wrong work area, hooks/settings, dependency install, dirty-tree mixing.
2. Behavioral risks: regressions, missing edge cases, broken contracts.
3. Verification gaps: missing or weak command evidence.
4. Commit readiness: diff scope, unrelated files, message availability.

## Rules

- Findings first, ordered by severity.
- Keep line/file references specific.
- Do not create PRs, push, merge, rebase, stash, reset, clean, or remove worktrees.
- Do not stage or commit unless the user already authorized it in the plan and the diff scope is clean.

## Output

If issues exist, list findings first. If no issues are found, say so and identify remaining test or review risk.
