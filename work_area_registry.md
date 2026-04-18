# Work Area Registry

This file registers the repository's work areas and the default write boundary for each one.

Default rules:

- Default read scope: the entire repository.
- Default write scope: the current target work area's directory tree only.
- The repository root is not a default business write area.
- New work areas must be registered here before implementation starts.
- Adding a new work area does not automatically justify a project-level `.codex/config.toml`.
- Adding a new work area does not automatically justify a work-area-specific `CLAUDE.md`.

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
- For Codex, start from the work area directory or use `codex --cd <work_area_path>`.
- For Codex, default to no `--add-dir`; only expand write scope when the user explicitly requires it.
- For Claude Code, start from the work area directory for implementation tasks.
- When starting from the repository root, limit activity to read-only exploration or repository governance.
- Do not create scraping directories, output directories, MCP directories, or business files at the repository root without explicit repository-governance intent.

## New Work Area Registration Template

| work_area | purpose | default_write_scope | read_scope | status | notes |
| :--- | :--- | :--- | :--- | :--- | :--- |
| `<path>/` | `<what this work area is for>` | `<path>/**` | `whole repo` | `candidate/active` | `<constraints, dependencies, or handoff notes>` |
