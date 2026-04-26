# claudecode

`claudecode` is a multi-work-area development repository. It contains repo governance files plus independent coding/data work areas such as `run/`, `sm2-randomizer/`, `QuantProject/`, `heybox/`, `qm-run-demo/`, and `subtitle_extractor/`.

The repository root is the workflow entry and routing layer. It is not the default business implementation directory.

## Claude Code Read Order

1. `CLAUDE.md`
2. `AGENTS.md`
3. `PROJECT.md`
4. `work_area_registry.md`
5. `agent_tooling_baseline.md`
6. Relevant `docs/*.md`

`.claude/` stores repo-local settings, skills, hooks, and tools. It is not a replacement for the root entry files.

## Default Flow

- Small task: identify target work area, inspect narrow context, patch, run nearest verification, report.
- Medium task: write a brief plan, confirm assumptions when needed, patch in a narrow range, verify, report.
- Large task: narrow requirements, split work, decide whether a worktree is needed, decide whether TDD is needed, optionally use subagents for bounded side work, and close with PR-style review.
- For large tasks, a lightweight brainstorm / option comparison may happen before the detailed plan to narrow direction; brainstorm itself does not replace the acceptance plan, task split, or verification.

Use `docs/task-routing.md` for the detailed decision table.

## Boundaries

- This repo does not inherit `kb` ingest/wiki workflow.
- Do not modify `C:\Users\apple\kb` from this repo workflow.
- Do not modify global Claude Code, Codex, Superpowers, ECC, CLI, VS plugin, Codex App, Codex Proxy, global hooks, global skills, or global AGENTS/CLAUDE files from this repo workflow.
- Do not create project-level `.codex/config.toml`, `.mcp.json`, `playwright-mcp/`, or other MCP directories.
- Do not enable full Superpowers SessionStart or ECC.

## Work Area Rule

Before business implementation, declare the `target_work_area` using `work_area_registry.md`.

If the task only changes repo governance files, the target is `repo-root-governance`. If the target is unclear, stay read-only and list candidates.

## Continuous Execution

For accepted multi-step plans, use `.tmp/active-task/current.md` only as a runtime ledger. It is ignored, not learning, not a rules source, and cannot authorize dangerous operations.

Continue through already approved safe steps. Stop for blockers, scope changes, unclear dirty-tree ranges, dangerous git operations, global/kb boundary risk, dependency installation, or unclear user intent.

## Useful Entry Docs

```powershell
Get-Content .\work_area_registry.md
Get-Content .\docs\task-routing.md
Get-Content .\docs\safety-boundaries.md
Get-Content .\docs\continuous-execution.md
git status --short --branch
```
