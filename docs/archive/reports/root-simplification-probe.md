# Root Simplification Probe

## Scope

- Probe time: 2026-05-15 Asia/Singapore
- Repo: `C:\Users\apple\claudecode`
- Goal: simplify root layout, move runtime state into `run/workflow/`, and keep CC -> CX entrypoints intact.
- Sensitive boundary: no auth files, tokens, cookies, or full API keys were read or printed.

## 1. Git Status

`git status --short --untracked-files=all` before this cleanup phase:

```text
 M .gitignore
 D .task-worktree.json
 D CLAUDE_REVIEW.md
 D CODEX_RESULT.md
 M PROJECT.md
 M README.md
 M cx-exec.ps1
 M docs/workflows/10-cc-cx-orchestration.md
 M scripts/workflow/cx-exec.ps1
?? .workflow/reports/cc-cx-one-shot-refactor-report.md
?? .workflow/reports/current-state-before-refactor.md
?? docs/workflows/cc-cx-collaboration.md
```

This is the pending state from the previous CC -> CX refactor; no commit or push has been run.

## 2. Root Items

Current root-level items:

```text
.agents
.claude
.codex
.codex-exec-apple
.git
.githooks
.learnings
.workflow
docs
heybox
qm-run-demo
QuantProject
run
scripts
sm2-randomizer
subtitle_extractor
tests
.gitignore
agent_tooling_baseline.md
AGENTS.md
CLAUDE.md
cx-exec.ps1
PROJECT.md
README.md
work_area_registry.md
```

Root items that should not remain after cleanup: `.codex-exec-apple`, `.workflow`, `tests/workflow`, `.learnings`, root `agent_tooling_baseline.md`, root `work_area_registry.md`, and `docs/workflows/99-pipeline-smoke-test.md`.

## 3. `.codex-exec-apple`

- Exists: yes
- File count: 2636
- Size: 18074984 bytes
- Top-level contents: `.tmp`, `memories`, `sessions`, `skills`, `tmp`, `installation_id`, `logs_2.sqlite*`, `state_5.sqlite*`
- Reference probe found only old report/session references and no current script dependency.
- Classification: old relative `CODEX_HOME` runtime residue.
- Planned action: move to `run/workflow/archive/obsolete-codex-exec-apple-<timestamp>/`.
- Must not touch: `C:\Users\apple\.codex-exec`.

## 4. `.workflow`

- Exists: yes
- File count: 14
- Size: 49106 bytes
- Contents:
  - `archive/root-cleanup-20260515-222148/`
  - `reports/cc-cx-one-shot-refactor-report.md`
  - `reports/codex-missing-context-report.md`
  - `reports/current-state-before-refactor.md`
  - `tasks/smoke-dryrun/`
  - `tasks/smoke-real/`
- Current script references: `scripts/workflow/cx-exec.ps1` still uses `.workflow`.
- Current docs references: README, PROJECT, `docs/workflows/cc-cx-collaboration.md`, `docs/workflows/10-cc-cx-orchestration.md`, and previous reports.
- Classification: workflow runtime and report store from the prior refactor.
- Planned action: merge into `run/workflow/`, update active references, and remove root `.workflow`.

## 5. `tests/workflow`

- Exists: yes
- File count: 1
- File: `tests/workflow/cx-exec.Tests.ps1`
- Content checks show it tests the older `cx-exec.ps1` function API (`Invoke-CxExec`, `Ensure-CcCodexConfig`, `CODEX_PROMPT.md`, `CODEX_RESULT.md`).
- Classification: workflow script test, not a repository-wide test system.
- Planned action: move under `scripts/workflow/tests/` and update it to match the new CLI/dry-run behavior.

## 6. `.learnings`

- Exists: yes
- File count: 3
- Files: `ERRORS.md`, `FEATURE_REQUESTS.md`, `LEARNINGS.md`
- Content references mention old failure hooks and learning promotion, but no current hard dependency was found in active scripts.
- Classification: learning/error notes, not runtime entrypoint.
- Planned action: move tracked learning notes to `docs/learnings/`, remove root `.learnings`, and update ignore rules.

## 7. Root Governance Files

- `agent_tooling_baseline.md`: exists in root, tracked.
- `work_area_registry.md`: exists in root, tracked.
- Existing docs equivalents:
  - `docs/workflows/agent_tooling_baseline.md`: missing.
  - `docs/workflows/work_area_registry.md`: missing.
- References found in README, PROJECT, AGENTS, docs, and skill docs.
- Planned action: move both to `docs/workflows/` and update active references.

## 8. Entry File References

- `README.md` still references root `work_area_registry.md`, root `agent_tooling_baseline.md`, and `.workflow/tasks/<task_id>/`.
- `PROJECT.md` still references root `work_area_registry.md`, root `agent_tooling_baseline.md`, `.workflow/`, and `.learnings/`.
- `AGENTS.md` still references root `work_area_registry.md`, root `TASK_HANDOFF.md`, and root `.task-worktree.json`.
- `CLAUDE.md` still says CC should call `scripts/workflow/cx-exec.ps1` and mentions an independent `CODEX_HOME` wording that should be aligned with the wrapper-first path.

## 9. Mock / Smoke Artifacts

- `docs/workflows/99-pipeline-smoke-test.md`: exists and is tracked.
- Root `CODEX_RESULT.md`: absent.
- Root `CLAUDE_REVIEW.md`: absent.
- Root `TASK_HANDOFF.md`: absent.
- Root `diagnosis.log`: absent.
- Root `.task-worktree.json`: absent.

Planned action: remove `docs/workflows/99-pipeline-smoke-test.md`; it is a generated mock smoke artifact, not a formal workflow document.

## 10. Decision

- Continue cleanup: yes.

Reason:

- The disputed root items are either runtime residues, old learning notes, workflow-only tests, or root governance files with clear destinations.
- No forbidden independent repo directory needs to move.
- CC -> CX entrypoints can be preserved while changing the runtime root from `.workflow` to `run/workflow`.
- No credential files need to be read.
