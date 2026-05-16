# CC -> CX One-shot Refactor Report

## 1. Summary

- Fresh probe completed first; previous report was not reused as current fact.
- Root `cx-exec.ps1` was a mock/smoke script and is now a lightweight delegator.
- `scripts/workflow/cx-exec.ps1` is now the real CC -> Codex executor.
- CC execution results now go under `.workflow/tasks/<task_id>/`.
- Root runtime artifacts were migrated to `.workflow/archive/root-cleanup-20260515-222148/`.
- VS Code C# wrapper was inspected but not modified.
- No branch was created, no commit was made, no push was run.

## 2. Current State After Fresh Probe

Fresh probe facts before refactor:

- Repo root: `C:/Users/apple/claudecode`
- Branch: `main`
- Latest commit: `e33ca6e 1`
- Initial `git status --short --untracked-files=all`: clean
- Root `cx-exec.ps1`: classified as mock
- `scripts/workflow/cx-exec.ps1`: older real executor, but not wrapper-first and still wrote root-style `CODEX_RESULT.md`
- Wrapper:
  - `C:\Users\apple\codex-maintenance\codex-exec-wrapper.cs`: exists
  - `C:\Users\apple\codex-maintenance\codex-exec-wrapper.exe`: exists
  - wrapper version: `codex-cli 0.130.0-alpha.5`
  - real Codex exe: `C:\Users\apple\AppData\Local\OpenAI\Codex\bin\codex.exe`
  - wrapper `CODEX_HOME`: `C:\Users\apple\.codex-exec`
- Config:
  - `C:\Users\apple\.codex-exec\config.toml`: exists
  - repo-local `.codex/config.toml`: missing
  - provider: `codex-proxy`
  - base URL: `http://127.0.0.1:8080/v1`
  - env key: `CODEX_PROXY_API_KEY`
  - wire API: `responses`
- Proxy:
  - `CODEX_PROXY_API_KEY`: set
  - health: `status=ok`, `authenticated=true`, `pool.total=9`, `pool.active=9`

Full probe report: `.workflow/reports/current-state-before-refactor.md`.

## 3. Execution Decision

- Continue refactor: yes.

Reason:

- Git tree was clean before refactor.
- Target files were clear and did not overlap unknown user changes.
- Wrapper existed and could remain read-only.
- Proxy/config were healthy enough for real smoke.
- Directory migration only touched explicit root temporary/runtime artifacts.
- The five independent repo directories were not moved.

## 4. Files Changed

- Modified:
  - `.gitignore`
  - `README.md`
  - `PROJECT.md`
  - `cx-exec.ps1`
  - `scripts/workflow/cx-exec.ps1`
  - `docs/workflows/10-cc-cx-orchestration.md`
- Added:
  - `.workflow/reports/current-state-before-refactor.md`
  - `.workflow/reports/cc-cx-one-shot-refactor-report.md`
  - `docs/workflows/cc-cx-collaboration.md`
- Moved to ignored archive:
  - `CODEX_RESULT.md`
  - `CLAUDE_REVIEW.md`
  - `diagnosis.log`
  - `.task-worktree.json`
- Skipped because missing:
  - `TASK_HANDOFF.md`

Archive manifest:

- `.workflow/archive/root-cleanup-20260515-222148/manifest.json`

## 5. Root Directory Before/After

Before refactor, root included:

- `.task-worktree.json`
- `CLAUDE_REVIEW.md`
- `CODEX_RESULT.md`
- `diagnosis.log`

After refactor, root no longer contains those runtime artifacts. Current root still contains:

- fixed retain: `.agents`, `.claude`, `.codex`, `.git`, `.githooks`, `.learnings`, `docs`, `run`, `scripts`, `tests`, `.gitignore`, `AGENTS.md`, `CLAUDE.md`, `PROJECT.md`, `README.md`
- independent repos not moved: `heybox`, `qm-run-demo`, `QuantProject`, `sm2-randomizer`, `subtitle_extractor`
- governance entry files retained: `cx-exec.ps1`, `agent_tooling_baseline.md`, `work_area_registry.md`
- workflow directory: `.workflow`

`agent_tooling_baseline.md` and `work_area_registry.md` were kept in root because `README.md`, `PROJECT.md`, `AGENTS.md`, and docs still reference them as root-level entry files. Moving them now would require broader link and rule updates.

## 6. Workflow Entrypoints

- Codex independent mode:
  - Direct Codex task execution follows `AGENTS.md`, `PROJECT.md`, `work_area_registry.md`, and the user prompt.
  - It does not require `cx-exec.ps1`.
- CC call mode:
  - Root delegator: `.\cx-exec.ps1`
  - Real executor: `scripts/workflow/cx-exec.ps1`
  - Wrapper: `C:\Users\apple\codex-maintenance\codex-exec-wrapper.exe`
  - Result path: `.workflow/tasks/<task_id>/result.json`
  - Logs:
    - `.workflow/tasks/<task_id>/codex.log`
    - `.workflow/tasks/<task_id>/codex.err.log`

Supported parameters:

- `-TaskId`
- `-TaskDescription`
- `-Profile design|implement|review|lint|full-access`
- `-DryRun`

## 7. CODEX_HOME / Wrapper / Proxy Validation Result

- Default `CODEX_HOME` for CC executor: `C:\Users\apple\.codex-exec`
- Wrapper-first behavior: yes, executor calls `C:\Users\apple\codex-maintenance\codex-exec-wrapper.exe`
- PATH fallback to npm `codex`: no
- Full API key printed: no
- Preflight checks implemented:
  - wrapper exists
  - `C:\Users\apple\.codex-exec\config.toml` exists
  - `CODEX_PROXY_API_KEY` set/missing only
  - proxy health status/authenticated
- Real smoke showed Codex banner with:
  - version: `0.130.0-alpha.5`
  - provider: `codex-proxy`
  - model: `gpt-5.5`

Note: real smoke stderr included ChatGPT plugin-sync warnings/403 for remote plugins, but the requested repository check succeeded and exited 0.

## 8. DryRun Result

Command:

```powershell
.\cx-exec.ps1 -TaskId "smoke-dryrun" -TaskDescription "Say hello from Codex dry run" -Profile implement -DryRun
```

Result:

- exit code: 0
- result file: `.workflow/tasks/smoke-dryrun/result.json`
- status: `success`
- summary: `DryRun completed; Codex was not invoked.`
- root `CODEX_RESULT.md` regenerated: no

## 9. Real Smoke Result

Command:

```powershell
.\cx-exec.ps1 -TaskId "smoke-real" -TaskDescription "Inspect the repository root and report only whether README.md and PROJECT.md exist. Do not modify files." -Profile review
```

Result:

- exit code: 0
- result file: `.workflow/tasks/smoke-real/result.json`
- status: `success`
- duration: `16.854` seconds
- `codex.log` final answer:
  - `README.md exists: yes`
  - `PROJECT.md exists: yes`
- business files modified by smoke: no

Operational note:

- First real smoke attempt hit a 180-second outer tool timeout before script timeout support existed. The launched wrapper/Codex child processes from that attempt were stopped, then the executor was updated with a 120-second internal timeout and rerun successfully.

## 10. Git Status

Final `git status --short --untracked-files=all`:

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
?? .workflow/reports/current-state-before-refactor.md
?? .workflow/reports/cc-cx-one-shot-refactor-report.md
?? docs/workflows/cc-cx-collaboration.md
```

`diagnosis.log` was ignored by existing `*.log` ignore rule, so its archive move does not appear in Git status.

## 11. Remaining Risks

- `.workflow/archive/` is ignored by design, so archived copies are local runtime evidence, not tracked review artifacts.
- The three tracked root artifacts now appear as deletions; this is intentional root cleanup, but CC should explicitly accept it before commit.
- `agent_tooling_baseline.md` and `work_area_registry.md` remain in root to avoid broad link churn.
- Real smoke proved the wrapper path can execute Codex through this script, but stderr still includes remote plugin sync warnings unrelated to the requested repo inspection.
- Existing workflow docs still contain older worktree-era concepts; this refactor updated only the CC -> CX handoff surface.

## 12. Exact Next Command For CC Final Verification

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -Command "Get-Content .workflow\\reports\\cc-cx-one-shot-refactor-report.md; Get-Content .workflow\\tasks\\smoke-real\\result.json; git status --short --untracked-files=all"
```
