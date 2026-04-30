# Work Area Registry

This file registers the repository's work areas and the default write boundary for each one.

Default rules:

- Default read scope: the entire repository.
- Default write scope: the current target work area's directory tree only.
- The repository root is not a default business write area.
- New work areas must be registered here before implementation starts.
- Adding a new work area does not automatically justify a project-level `.codex/config.toml`.
- Adding a new work area does not automatically justify a work-area-specific `CLAUDE.md`.
- These rules define default rule sources and write boundaries; they do not block normal read access to current-task files inside the current work area.

## Work Area Selection Protocol

1. Declare `target_work_area` before implementation; choose from the registered active rows below.
2. Declare `allowed_write_scope`; by default it is exactly the selected work area's `default_write_scope`.
3. Use `readonly_reference_scope` for cross-work-area context; whole-repo read is allowed unless the task narrows it.
4. If the target is unclear, stay read-only, inspect candidates, and report the likely work areas before writing.
5. Repository-root writes are allowed only for governance/control-plane files such as this registry, root entrypoints, or repo tooling docs.
6. A Git worktree is an execution surface with its own branch/merge path; it is not an active work area unless this registry says so.
7. A Claude Code transient worktree under `.claude/worktrees/**` is runtime/execution state; do not promote it to source of truth.
8. Directed/user worktrees use `wt-directed-<purpose>` at `C:\Users\apple\_worktrees\claudecode\directed\<purpose>` with repo-external registry markers `owner=user` / `protected=true`; they are persistent and never auto-cleaned.
9. Agent-created worktrees use `wt-auto-cc-<yyyymmdd-hhmm>-<purpose>-<id>` at `C:\Users\apple\_worktrees\claudecode\auto\<branch>` with repo-external registry markers `owner=agent`; `WorktreeRemove` may remove only clean owner=agent worktrees with non-force `git worktree remove <path>` and must retain branches.
10. Do not create nested linked worktrees, `worktree-agent-*`, random adjective names, or bare `agent-*`; `worktree-run-scraping-refactor-phase1` is grandfathered keep.

## Registered Work Areas

| work_area | purpose | default_write_scope | read_scope | status | notes |
| :--- | :--- | :--- | :--- | :--- | :--- |
| `run/` | Hextech primary runtime, processing, scraping, display, and packaged assets | `run/**` | whole repo | active | Treat as the baseline work area for current Hextech tasks |
| `heybox/` | Heybox scraping scripts and related local docs/data | `heybox/**` | whole repo | active | Small standalone scraper work area |
| `qm-run-demo/` | Demo/runtime variant with its own nested `run/`, docs, and repo metadata | `qm-run-demo/**` | whole repo | active | Keep writes inside the top-level demo tree |
| `QuantProject/` | Quant strategy, data sync, reports, and position history | `QuantProject/**` | whole repo | active | Independent data/model work area |
| `sm2-randomizer/` | Space Marine 2 randomizer app, pipeline, docs, and debug assets | `sm2-randomizer/**` | whole repo | active | Keep generated outputs inside this tree |
| `subtitle_extractor/` | Subtitle extraction scripts, requirements, and local project docs | `subtitle_extractor/**` | whole repo | active | Standalone media-processing work area |

## Operating Notes

- Pick the target work area before writing files.
- When the target is unclear, stay read-only and list candidate work areas first.
- For Claude Code, treat it as the daily primary development endpoint for this repository and start from the selected work area directory for implementation tasks.
- For Codex / CX App, default to global or cross-repository capability work, refactor assistance, personal-assistant tasks, and cloud PR review; it is not a mandatory local task dispatcher for this repository.
- Web AI is reference-only for requirements, knowledge gathering, and second opinions.
- Antigravity is manual-only for Claude Opus 4.6 independent review or Gemini hard frontend delivery; do not enable an Antigravity gate.
- When starting from the repository root, limit activity to read-only exploration or repository governance.
- Do not create scraping directories, output directories, MCP directories, or business files at the repository root without explicit repository-governance intent.
- For repository-governance tasks, do not treat Desktop / OneDrive support-layer documents as default reference sources unless the user explicitly asks for audit, comparison, or migration work.
- `.ai_workflow`, lowercase `agents.md`, `finish-task`, `event_log`, and `active_tasks_index` are not active work-area mechanisms.

## New Work Area Registration Template

| work_area | purpose | default_write_scope | read_scope | status | notes |
| :--- | :--- | :--- | :--- | :--- | :--- |
| `<path>/` | `<what this work area is for>` | `<path>/**` | `whole repo` | `candidate/active` | `<constraints, dependencies, or handoff notes>` |
