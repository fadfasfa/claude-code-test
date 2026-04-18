<!-- locally-authored-minimal -->
---
name: verification-before-completion
description: Require concrete verification evidence before claiming a task is complete.
---

# verification-before-completion

Use this skill before marking any implementation task as done.

## Required behavior

1. Run the narrowest relevant verification commands for the change.
2. Inspect the output rather than assuming success.
3. If verification cannot run, say exactly why.
4. In the final summary, include the verification evidence or blocker.

## Rules

- Never claim completion without evidence.
- Prefer repo-native commands and existing test scripts.
- Do not add hooks, MCP requirements, worktrees, or forced branch logic.
