# Current State Before CC -> CX Refactor

## Probe Scope

- Probe time: 2026-05-15 Asia/Singapore
- Repo root: `C:\Users\apple\claudecode`
- Mode: fresh probe after cloud pull; older reports were treated as historical reference only.
- Sensitive boundary: no auth files, tokens, cookies, API keys, or proxy secrets were printed.

## 1. Git State

- `git rev-parse --show-toplevel`: `C:/Users/apple/claudecode`
- `git branch --show-current`: `main`
- `git log -1 --oneline`: `e33ca6e 1`
- `git status --short --untracked-files=all`: clean before refactor.

### Uncommitted Change Classification

- Old temporary artifacts: none uncommitted before refactor.
- Files safe for this task to migrate if present: tracked root runtime artifacts `CODEX_RESULT.md`, `CLAUDE_REVIEW.md`, `.task-worktree.json`; ignored root log `diagnosis.log`.
- Possible user/cloud new changes that must not be overwritten: none shown by Git before refactor.

## 2. Root Directory Structure

Observed with `Get-ChildItem -Force -Name`:

- Fixed retain: `.agents`, `.claude`, `.codex`, `.git`, `.githooks`, `.learnings`, `docs`, `run`, `scripts`, `tests`, `.gitignore`, `AGENTS.md`, `CLAUDE.md`, `PROJECT.md`, `README.md`.
- Independent repo directories, do not move: `heybox`, `qm-run-demo`, `QuantProject`, `sm2-randomizer`, `subtitle_extractor`.
- Workflow entrypoints / governance files: `cx-exec.ps1`, `agent_tooling_baseline.md`, `work_area_registry.md`.
- Temporary or runtime artifacts: `.codex-exec-apple`, `.workflow`, `.task-worktree.json`, `CLAUDE_REVIEW.md`, `CODEX_RESULT.md`, `diagnosis.log`.
- Uncertain items: none requiring stop.

Default root-retained files confirmed: `README.md`, `PROJECT.md`, `AGENTS.md`, `CLAUDE.md`, `.gitignore`.

## 3. CX Entry State

- `C:\Users\apple\claudecode\cx-exec.ps1`: exists.
- `C:\Users\apple\claudecode\scripts\workflow\cx-exec.ps1`: exists.

Hashes:

- `C:\Users\apple\claudecode\cx-exec.ps1`: `8A4217D9A9C1F39F965436B4FA5D2C1CF8053FAF6FD559016F18AB774F8AF21A`
- `C:\Users\apple\claudecode\scripts\workflow\cx-exec.ps1`: `EB146D02820DC49340DE93EB3B2C8E3726EEA974357A6A9451789261BB7E9902`

Pattern probe:

- Root `cx-exec.ps1` contains `Simulates`, `Set-Content`, `CODEX_RESULT.md`, and `99-pipeline-smoke-test`.
- Root `cx-exec.ps1` does not contain `codex exec`, `Start-Process`, or `codex-exec-wrapper.exe`.
- `scripts/workflow/cx-exec.ps1` contains `CODEX_RESULT.md`, `codex exec`, and `Set-Content`.
- `scripts/workflow/cx-exec.ps1` does not contain `codex-exec-wrapper.exe`.

Current root `cx-exec.ps1` classification: `mock`.

Current `scripts/workflow/cx-exec.ps1` classification: older real executor, but not the required wrapper-first structured-result implementation. It writes root-style `CODEX_RESULT.md`, expects `CODEX_PROMPT.md`, and currently enforces task worktree checks.

## 4. Wrapper State

- `Test-Path C:\Users\apple\codex-maintenance\codex-exec-wrapper.cs`: true.
- `Test-Path C:\Users\apple\codex-maintenance\codex-exec-wrapper.exe`: true.
- `C:\Users\apple\codex-maintenance\codex-exec-wrapper.exe --version`: `codex-cli 0.130.0-alpha.5`

Wrapper source non-sensitive facts:

- Real Codex executable path: `C:\Users\apple\AppData\Local\OpenAI\Codex\bin\codex.exe`
- Wrapper `CODEX_HOME`: `C:\Users\apple\.codex-exec`
- Source sets `startInfo.EnvironmentVariables["CODEX_HOME"] = CodexHome`.
- Source has no match for deleting or overwriting `CODEX_PROXY_API_KEY`.

Decision: do not modify wrapper; preserve VS Code Codex wrapper chain.

## 5. Codex Config State

- `Test-Path C:\Users\apple\.codex-exec\config.toml`: true.
- `Test-Path C:\Users\apple\claudecode\.codex\config.toml`: false.

Non-sensitive fields extracted from `C:\Users\apple\.codex-exec\config.toml`:

```toml
model = "gpt-5.5"
model_provider = "codex-proxy"
[model_providers.codex-proxy]
base_url = "http://127.0.0.1:8080/v1"
env_key = "CODEX_PROXY_API_KEY"
wire_api = "responses"
[profiles.design]
model = "gpt-5.5"
model_provider = "codex-proxy"
[profiles.implement]
model = "gpt-5.5"
model_provider = "codex-proxy"
[profiles.review]
model = "gpt-5.5"
model_provider = "codex-proxy"
[profiles.lint]
model = "gpt-5.5"
model_provider = "codex-proxy"
[profiles.full-access]
```

Repo-local `.codex/config.toml` does not exist, so no repo-local Codex config override was found.

## 6. Environment And Proxy State

- `CODEX_PROXY_API_KEY`: set.
- `Invoke-RestMethod http://127.0.0.1:8080/health`: `status=ok`, `authenticated=true`, `pool.total=9`, `pool.active=9`.

Proxy is healthy enough to permit a conditional real smoke after script refactor.

## 7. Existing `.workflow/`

Before this refactor:

- `.workflow/`: exists.
- `.workflow/reports/`: exists.
- `.workflow/tasks/`: missing.
- `.workflow/current/`: missing.
- `.workflow/state/`: missing.
- `.workflow/archive/`: missing.
- `.gitignore` has no `.workflow` runtime ignore rules.

No conflicting `.workflow` structure was found.

## 8. `docs/workflows/`

- `docs/workflows/`: exists.
- `docs/workflows/cc-cx-collaboration.md`: missing.
- `docs/workflows/06-codex-proxy-policy.md`: exists.
- `docs/workflows/agent_tooling_baseline.md`: missing.
- `docs/workflows/work_area_registry.md`: missing.
- Existing related doc: `docs/workflows/10-cc-cx-orchestration.md`.
- Existing generated smoke doc: `docs/workflows/99-pipeline-smoke-test.md`.

Existing workflow docs should be updated incrementally, not rewritten wholesale.

## Execution Decision

- Continue refactor: yes.

Rationale:

- Git status was clean before refactor; no unknown user/cloud modifications overlap with the target files.
- Root `cx-exec.ps1` is clearly a mock and can be replaced by a lightweight delegator.
- `scripts/workflow/cx-exec.ps1` is understandable and can be replaced with the requested wrapper-first implementation.
- Wrapper exists and will not be modified.
- Codex config can be checked without reading secrets.
- Proxy health is ok; real smoke can be attempted after the dry run.
- Directory migration is limited to explicit root temporary/runtime artifacts.
- The five independent repo directories will not be moved.
- `README.md`, `PROJECT.md`, `AGENTS.md`, and `CLAUDE.md` will not be rewritten wholesale.

Stop conditions checked:

- Current `cx-exec.ps1` is not a complex cloud implementation; it is a mock.
- Current `scripts/workflow/cx-exec.ps1` is older workflow logic but not ambiguous enough to stop.
- No large unknown Git changes overlap with this task.
- No token/auth file read is required.
- Proxy/config state is healthy and distinguishable from script issues.
- VS Code wrapper chain is read-only for this task.
