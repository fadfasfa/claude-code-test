# Agent Tooling Baseline

This file is the repo-side capability baseline note, not the global source of truth.

Global CC / CX skill / CLI / rule / hook facts are maintained in:

- `C:\Users\apple\OneDrive\Desktop\各个设定及工作流\_support\cc_cx_skill_cli_inventory.md`

## Claude Code

- Global entry: `C:\Users\apple\.claude\CLAUDE.md`
- Global proxy entry: `C:\Users\apple\.claude\settings.json`
- Repo entry: `C:\Users\apple\claudecode\CLAUDE.md`
- Global skills and hooks: see the inventory above.
- Repo skills:
  - `api-design`
  - `backend-patterns`
  - `frontend-patterns`
  - `python-patterns`
  - `python-testing`
  - `requesting-code-review`
  - `verification-before-completion`
- Karpathy perspective skill belongs to Claude Code global only; this repo only keeps repo-specific skills.
- Disabled by baseline: plugins = 0, MCP = 0 at repo layer.
- Repo env disables background/fork worktree spawning with `CLAUDE_CODE_DISABLE_BACKGROUND_TASKS=1` and `CLAUDE_CODE_FORK_SUBAGENT=0`.
- Active repo-local shell hooks: `WorktreeCreate` naming guard, `PreToolUse` unsafe shell command block, and `PostToolUseFailure` self-improvement error capture.
- Shared safety boundaries live in `.claude/settings.json`; `.claude/settings.local.json` is local-only and only carries this machine's execution allowlist / override.
- Execution permissions: local settings auto-allow safe read/status/dry-run commands, while destructive cleanup, network install/download, process control, and global config writes remain denied or explicit-confirmation actions.
- Worktree creation helper: `.claude/tools/worktree-governor/new_worktree.ps1`; cleanup / janitor / remove are not default mechanisms.
- `.learnings/LEARNINGS.md` is the tracked self-improvement evolution log; `.learnings/ERRORS.md` is ignored local/raw error input, not the main entry.
- Self-improvement focuses on CC skill improvement inside this repo, may propose changes, and may execute approved repo-local CC skill changes in a user-authorized task.
- Self-improvement may propose rule, skill, hook, and global sync changes; repo-local CC skill changes may execute after user approval, while global sync requires human review before synchronization.
- Self-improvement does not modify `kb` by default, does not interfere with CX App / Codex memory, and must log every evolution under `.learnings/LEARNINGS.md`.
- `kb` will need a separate reduced self-improvement mechanism later.
- This repository does not define an active proxy endpoint for Claude Code.
- Claude Code proxy wiring is inherited from global `~/.claude/settings.json`.
- Do not write `6984`, `HTTPS_PROXY`, or `tls.proxy_url` into this repository's active entry files.

## Codex

- Global instruction root: `C:\Users\apple\AGENTS.md`
- Global config: `C:\Users\apple\.codex\config.toml`
- Global skills / discipline / compat layering: see the inventory above.
- Repo defaults:
  - no project-level `.codex/config.toml`
  - plugins = 0
  - MCP = 0
  - hooks = 0
  - no automatic worktree
  - no automatic branch creation
- Codex reads repo entry docs and workspace docs, but does not rely on repo-local method skills.
- Codex / CX App is a global personal assistant for multi-thread / multi-repo workflow, rule, refactor, review, and convergence work; in this repo it avoids CC implementation tasks unless explicitly requested.

## Workspace Boundaries

- `claudecode` is a multi-work-area repository.
- Rule-source priority and write boundaries are defined in repo-root `AGENTS.md`, `PROJECT.md`, and `work_area_registry.md`.
- This file records capability constraints only: no project-level `.codex/config.toml`, no `.mcp.json`, and no per-work-area rule files by default.
- Task planning defaults to Claude Code Plan Mode and local execution. Gemini / Claude / GPT web AI is an external validation layer for retrieval, idea checks, and result review; it is not Codex Cloud, a dispatcher, an execution source, or the final decision layer.
- PR / diff review may be done locally by Claude Code using `final_review_contract.md`; Codex review adapter applies only when the route explicitly asks for Codex review.
