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

## Workspace Boundaries

- `claudecode` is a multi-work-area repository.
- Rule-source priority and write boundaries are defined in repo-root `AGENTS.md`, `PROJECT.md`, and `work_area_registry.md`.
- This file records capability constraints only: no project-level `.codex/config.toml`, no `.mcp.json`, and no per-work-area rule files by default.
