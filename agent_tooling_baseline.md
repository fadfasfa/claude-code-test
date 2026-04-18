# Agent Tooling Baseline

This file is the Phase 1 single source of truth for capability-layer setup only. It does not replace workflow contracts.

## Claude Code

- Global entry: `C:\Users\apple\.claude\CLAUDE.md`
- Repo entry: `C:\Users\apple\claudecode\CLAUDE.md`
- Global skills:
  - `brainstorming`
  - `systematic-debugging`
  - `search-first`
- Repo skills:
  - `verification-before-completion`
  - `requesting-code-review`
  - `frontend-patterns`
  - `backend-patterns`
  - `api-design`
  - `python-patterns`
  - `python-testing`

## Codex

- Global entry: `C:\Users\apple\.codex\AGENTS.md`
- Global skills:
  - `brainstorming`
  - `systematic-debugging`
  - `verification-before-completion`
  - `repo-scan-and-plan`
  - `diff-self-review`
  - `search-first`
- Core operating defaults:
  - CLI-first
  - MCP-last
  - no hooks
  - no global MCP
  - no automatic worktree
  - no automatic branch creation

## Workspace Write Boundaries

- `claudecode` is a multi-work-area repository.
- Claude and Codex may read across the repository by default.
- Write access is scoped to the current target work area unless the user explicitly expands scope.
- The repository root is for governance files, not default business output.
- Do not add `.mcp.json`, project-level Codex config, or per-work-area rule files by default.

## Disabled or Deferred

- Claude Code plugins
- Codex plugins
- Global MCP servers
- Hooks
- Automatic worktree creation
- Automatic branch creation for every large task
- TDD activation

## Proxy and Provider Handling

- Preserve current working provider/auth/proxy wiring.
- Do not hardcode new proxy endpoints into global templates.
- Keep proxy examples in documentation-only template files.

## TDD

- TDD is interface-only in Phase 1.
- Reserved names:
  - `tdd-workflow`
  - `test-driven-development`
  - `red-green-refactor`

## Antigravity

- Current status: manual lane only.
- Allowed uses:
  - explicit frontend refactor requests
  - explicit review requests
  - explicit text-assistance requests
- Not part of default routing, gate, or automatic dispatch.
