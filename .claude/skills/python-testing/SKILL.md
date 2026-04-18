<!-- adapted-from-upstream: C:\Users\apple\.agents\skills\python-testing\SKILL.md -->
---
name: python-testing
description: Add or update Python tests, run them, and provide verification evidence without enforcing strict TDD.
---

# python-testing

Use this skill when Python behavior changes need coverage.

## Required behavior

1. Add or update the smallest useful test coverage for the changed behavior.
2. Run the relevant test command locally.
3. Report the exact test evidence or blocker.

## Rules

- This phase does not enforce strict TDD.
- Focus on regression protection and reproducible verification.
- Do not introduce hooks, MCP dependencies, worktree logic, or forced branch policy.
