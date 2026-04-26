# Agent Tooling Baseline

This file records the repo-local capability baseline for `claudecode`. It is not the global source of truth and must not be used to modify global Claude Code, Codex, Superpowers, ECC, CLI, VS plugin, Codex App, or Codex Proxy configuration.

## Scope

- Repo scope: `C:\Users\apple\claudecode`
- Boundary reference only: `C:\Users\apple\kb`
- Forbidden from this repo workflow unless the user starts a separate global task: `C:\Users\apple\.claude`, `C:\Users\apple\.codex`, global hooks, global skills, global AGENTS/CLAUDE files, CLI installs, VS plugins, Codex App, Codex Proxy, global Superpowers/ECC installs.

## Claude Code

- Repo entry: `C:\Users\apple\claudecode\CLAUDE.md`
- Repo settings: `.claude/settings.json`
- Repo hooks currently active through settings:
  - `WorktreeCreate`: worktree naming guard
  - `PreToolUse`: unsafe raw shell/worktree command block
  - `PostToolUseFailure`: self-improvement raw error capture
- Hooks must remain safety or lightweight reminder mechanisms. They must not become schedulers, auto-continue engines, or business-file writers.
- Repo env disables background/fork worktree spawning with `CLAUDE_CODE_DISABLE_BACKGROUND_TASKS=1` and `CLAUDE_CODE_FORK_SUBAGENT=0`.

## Repo Skills

Existing repo-local skills:

- `api-design`
- `backend-patterns`
- `frontend-patterns`
- `python-patterns`
- `python-testing`
- `requesting-code-review`
- `verification-before-completion`

Workflow entry skills added for this repo layer:

- `resume-active-task`
- `module-admission`
- `frontend-polish-lite`
- `review-diff`
- `self-improvement-promotion`

These skills are on-demand workflow entries. They are not automatic SessionStart injections and are not inherited by `kb`.

## Task Weight

- Small tasks: no forced plan, TDD, worktree, subagent, or PR review.
- Medium tasks: short plan and verification; heavier tools only when risk justifies them.
- Large tasks: requirement narrowing, decomposition, optional worktree, optional TDD, optional subagent parallelism, and PR-style review.

Detailed routing lives in `docs/task-routing.md`.

## Worktree

- Detailed policy: `docs/git-worktree-policy.md`
- Worktree helper: `.claude/tools/worktree-governor/new_worktree.ps1`
- Worktree creation requires purpose and owner. Running without `-DryRun` requires explicit user instruction.
- Worktree removal always requires human confirmation.
- Legacy nested `.claude/worktrees/**` are not active workflow sources and must not be used as templates for new work.

## Subagents

Subagents are allowed only for bounded side work:

- read-only exploration
- test entrypoint discovery
- failure attribution
- review
- narrow implementation with disjoint file ownership when explicitly planned

Do not use concurrent subagents to edit the same file, shared settings, hooks, worktree policy, or shared data contracts.

## TDD and Superpowers

TDD is a coding-only option, not a default rule. Use it for high-risk behavior changes, regression-prone modules, or large tasks where tests are the clearest control surface.

Superpowers is not enabled as a default SessionStart workflow in this repository. Superpowers/TDD remains a task-scoped candidate only after module admission and user confirmation.

## Playwright and Frontend

Playwright CLI is claudecode-only and coding-only. It is not global core, not `kb`, not a global hook, and not a default step for all tasks.

Current machine discovery found a Playwright CLI on PATH, but this repository should not install dependencies, add Playwright config, or add frontend validation scripts without a module admission card.

Use `frontend-polish-lite` only for frontend tasks that need UI interaction, screenshot, responsive, visual, or accessibility checks.

## ECC

ECC is retired as a workflow source for this repository. This repo may inventory claudecode-local ECC residue, but must not edit global ECC installations or global settings. Any cleanup proposal must separate delete, archive, and backup candidates before file changes.

## Self-Improvement

- Tracked repo learning: `.learnings/LEARNINGS.md`
- Ignored raw error input: `.learnings/ERRORS.md`
- Runtime task ledger: `.tmp/active-task/current.md`

Raw logs and ledgers are not rules and must not be promoted automatically. Repo learning promotion uses the `self-improvement-promotion` skill and requires a user-reviewed checklist before changing tracked learning.

Self-improvement must not modify `kb` or global layers from this repo workflow.

## Codex

- Codex reads repo entry docs and workspace docs.
- No project-level `.codex/config.toml`.
- Repo layer keeps plugins = 0, MCP = 0, Codex hooks = 0.
- Codex does not create worktrees or branches automatically.
- Codex does not push, PR, merge, rebase, stash, reset, clean, or remove worktrees without explicit user confirmation.
