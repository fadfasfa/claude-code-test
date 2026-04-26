# claudecode Project Map

`claudecode` is a multi-functional local development repository. It is not a single application and not a knowledge-base workflow repository.

## Root Role

The repository root contains:

- Stable repo entry files: `AGENTS.md`, `CLAUDE.md`, `PROJECT.md`, `README.md`
- Work-area registry: `work_area_registry.md`
- Capability baseline: `agent_tooling_baseline.md`
- Repo-local workflow docs: `docs/`
- Repo-local Claude Code surfaces: `.claude/settings.json`, `.claude/hooks/`, `.claude/skills/`, `.claude/tools/`

Root files may be changed for repo governance and workflow refactoring. Business implementation belongs in the selected work area.

## Work Areas

| Path | Role |
| :--- | :--- |
| `run/` | Hextech runtime, scraping, processing, display, and tools |
| `sm2-randomizer/` | Space Marine 2 randomizer app and data pipeline |
| `QuantProject/` | Quant strategy/data workspace |
| `heybox/` | Local scraping scripts |
| `qm-run-demo/` | Demo/runtime variant workspace |
| `subtitle_extractor/` | Subtitle extraction workspace |
| `.claude/` | Repo-local Claude Code settings, skills, hooks, and tools |
| `.learnings/` | Repo-local learning log and ignored raw error inputs |
| `.tmp/` | Ignored runtime scratch space |
| `docs/` | Repo-local workflow, safety, routing, and validation policies |

Use `work_area_registry.md` as the stable registry before business writes.

## Workflow Docs

| File | Purpose |
| :--- | :--- |
| `docs/task-routing.md` | Small / medium / large task routing |
| `docs/safety-boundaries.md` | Repo, git, hook, global, and kb safety boundaries |
| `docs/module-admission.md` | Module admission card template and current module cards |
| `docs/continuous-execution.md` | Active-task ledger and resume/stop rules |
| `docs/frontend-validation.md` | Lightweight frontend validation workflow |
| `docs/playwright-policy.md` | claudecode-only Playwright policy |
| `docs/git-worktree-policy.md` | Detailed worktree classification and guardrails |

## Non-Goals

- Do not use this repo to refactor global Claude Code / Codex configuration.
- Do not use this repo to refactor `kb`.
- Do not promote coding-only workflow into `kb`.
- Do not make Superpowers, ECC, Playwright, TDD, worktree, PR, or subagent flow mandatory for every task.
