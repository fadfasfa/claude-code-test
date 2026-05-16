# Pre-Commit Dirty Tree Review

生成时间：2026-05-16

本报告只做提交前人工复核辅助。除生成本报告外，本轮未修改其他文件，未执行 `git add`、未 commit、未 push、未删除文件。

## Verdict

| 检查项 | 结论 |
| :--- | :--- |
| 是否建议继续重跑强验收 | yes |
| 是否发现不应提交内容 | yes |
| 是否发现误删风险 | yes |
| 是否发现旧路径仍作为当前规则存在 | no |
| 是否发现 worktree 边界冲突 | no |

## Read Inputs

- `.state/workflow/reports/repo-governance-audit.md`
- `.state/workflow/reports/repo-governance-repair-report.md`
- `docs/index.md`
- `docs/workflows/repository-layout.md`
- `docs/workflows/worktree-policy.md`
- `scripts/README.md`
- `scripts/workflow/README.md`

## Git State Summary

只读命令：

- `git status --short --untracked-files=all`
- `git diff --stat`
- `git diff --name-status`
- `git status --short --ignored=matching --untracked-files=all -- .state/workflow run/workflow .workflow .codex-exec-apple .learnings`

结果摘要：

- total status entries: 116
- modified: 15
- deleted: 68
- untracked: 33
- ignored workflow runtime entries: 4
- `git diff --stat`: tracked diff shows 83 files changed, 407 insertions, 7349 deletions. This does not include untracked additions.

## Deletion Review

Expected cleanup / migration deletions:

- `.codex-exec-apple/**`: expected deletion of old repo-local Codex runtime/cache. Contains sqlite/log/session/cache-like paths; committing deletion removes them from tracked tree, but do not stage any live replacement content.
- `.workflow/**`: expected retired workflow runtime deletion.
- `.learnings/LEARNINGS.md`: paired with `docs/reference/learnings/LEARNINGS.md`.
- `CODEX_RESULT.md`, `CLAUDE_REVIEW.md`, `.task-worktree.json`: expected root runtime deletion.
- `agent_tooling_baseline.md`: paired with `docs/workflows/agent_tooling_baseline.md`.
- `work_area_registry.md`: paired with `docs/workflows/work_area_registry.md`.
- `docs/continuous-execution.md`: paired with `docs/reference/policies/continuous-execution.md`.
- `docs/frontend-validation.md`: paired with `docs/reference/policies/frontend-validation.md`.
- `docs/module-admission.md`: paired with `docs/reference/policies/module-admission.md`.
- `docs/playwright-policy.md`: paired with `docs/reference/policies/playwright-policy.md`.
- `docs/safety-boundaries.md`: paired with `docs/reference/policies/safety-boundaries.md`.
- `docs/task-routing.md`: paired with `docs/reference/policies/task-routing.md`.
- `docs/workflows/01-worktree-policy.md`: paired with `docs/workflows/worktree-policy.md`.
- `docs/workflows/archives/2026-05-14_cc-cx-integration.md`: paired with `docs/archive/workflows/2026-05-14_cc-cx-integration.md`.
- `tests/workflow/cx-exec.Tests.ps1`: paired with `scripts/workflow/tests/cx-exec.Tests.ps1`.

Needs manual confirmation before staging deletion:

- `docs/workflows/99-pipeline-smoke-test.md`: deleted with no paired new path found in status.
- `.codex-exec-apple/**`: expected, but high-volume tracked runtime/cache deletion should be reviewed as a group before staging.

Absent root/runtime paths:

- `run/workflow`: absent
- `.workflow`: absent
- `.codex-exec-apple`: absent
- `.learnings`: absent
- `CODEX_RESULT.md`: absent
- `CLAUDE_REVIEW.md`: absent
- `TASK_HANDOFF.md`: absent
- `diagnosis.log`: absent
- `.task-worktree.json`: absent

## Addition Review

Expected / reasonable additions:

- `.state/workflow/reports/repo-governance-audit.md`
- `.state/workflow/reports/repo-governance-repair-report.md`
- `docs/index.md`
- `docs/archive/README.md`
- `docs/archive/reports/*.md`
- `docs/archive/workflows/2026-05-14_cc-cx-integration.md`
- `docs/reference/README.md`
- `docs/reference/learnings/README.md`
- `docs/reference/learnings/FEATURE_REQUESTS.md`
- `docs/reference/learnings/LEARNINGS.md`
- `docs/reference/policies/*.md`
- `docs/workflows/agent-skill-inventory.md`
- `docs/workflows/agent_tooling_baseline.md`
- `docs/workflows/cc-cx-collaboration.md`
- `docs/workflows/repository-layout.md`
- `docs/workflows/work_area_registry.md`
- `docs/workflows/worktree-policy.md`
- `scripts/README.md`
- `scripts/workflow/README.md`
- `scripts/workflow/tests/cx-exec.Tests.ps1`

Exclude / hold:

- `docs/archive/learnings-retired/ERRORS.md`: raw retired error log, 65KB. It may be useful locally, but should not be staged until manually inspected and explicitly accepted. This is the main "不应提交内容" finding.
- `.state/workflow/tasks/**`: ignored runtime; not shown as untracked, should not be staged manually.
- `.state/workflow/archive/**`, `.state/workflow/current/**`, `.state/workflow/state/**`, `.state/workflow/logs/**`: ignored runtime/state; should not be staged manually.

## Sensitive / Runtime Content Check

Path/status scan findings:

- `auth.json`: 0 changed/untracked matches.
- `token`: 0 changed/untracked matches.
- `cookie`: 0 changed/untracked matches.
- `cache`: 0 changed/untracked matches by name.
- `logs`: 3 matches, all deletions under `.codex-exec-apple/logs_2.sqlite*`.
- `.codex-exec*`: 51 matches, all deletions under `.codex-exec-apple/**`.
- `.workflow`: 1 match, deletion of `.workflow/reports/codex-missing-context-report.md`.
- `run/workflow`: 0 changed/untracked matches.
- `.state/workflow/tasks|archive|current|state|logs`: 0 changed/untracked matches; ignored runtime entries exist and should stay ignored.

Conclusion: no auth/token/cookie file additions were found. The risk is not live secret addition; the risk is accidentally staging raw historical error material or mishandling old runtime/cache deletions.

## Old Path Reference Classification

Command:

```powershell
rg "run/workflow|\.workflow|\.codex-exec-apple|\.learnings" .
```

Classification:

| Class | Examples | Verdict |
| :--- | :--- | :--- |
| Current rule error reference | none found | acceptable |
| Archived historical reference | `docs/archive/reports/**`, `docs/archive/workflows/**` | acceptable |
| Audit/repair report historical reference | `.state/workflow/reports/repo-governance-audit.md`, `.state/workflow/reports/repo-governance-repair-report.md` | acceptable |
| Negative warning / retired path note | `.gitignore`, `PROJECT.md`, `docs/workflows/repository-layout.md`, `scripts/workflow/README.md`, `scripts/workflow/tests/cx-exec.Tests.ps1` | acceptable |
| Needs repair | none found | none |

Active current docs now point runtime to `.state/workflow/`. Old paths remain only as historical evidence or explicit "do not recreate" warnings.

## Worktree Boundary Review

Command:

```powershell
rg "worktree|worktrees|git worktree|--worktree" . --glob "!.git/**"
```

Findings:

- CC startup auto-create evidence: no.
- Codex startup auto-create evidence: no.
- Current documentation conflict: no active conflict found.
- Canonical policy: yes, `docs/workflows/worktree-policy.md`.
- Actual creation scripts:
  - `scripts/workflow/worktree-start.ps1`: current manual workflow entry; default dry-run, `-Apply` creates detached worktree.
  - `scripts/git/ccw-new.ps1`: legacy/manual helper; not default workflow.
- Removal scripts:
  - `scripts/workflow/cleanup-worktree.ps1`: manual cleanup with safety checks.
  - `scripts/git/ccw-gc.ps1`: legacy/manual helper.

Conclusion: worktree boundary is coherent after repair. `.claude/worktrees/` is described as a local placeholder and not a controller.

## Suggested Staging List

Stage only after reviewing the exclusions below:

- Modified governance/entry docs:
  - `.agents/skills/README.md`
  - `.agents/skills/karpathy-project-bridge/SKILL.md`
  - `.agents/skills/repo-maintenance/SKILL.md`
  - `.agents/skills/superpowers-project-bridge/SKILL.md`
  - `.claude/README.md`
  - `.gitignore`
  - `AGENTS.md`
  - `CLAUDE.md`
  - `PROJECT.md`
  - `README.md`
  - `cx-exec.ps1`
  - `docs/workflows/00-overview.md`
  - `docs/workflows/05-pr-review-policy.md`
  - `docs/workflows/10-cc-cx-orchestration.md`
  - `scripts/workflow/cx-exec.ps1`
- New reports / indexes / docs:
  - `.state/workflow/reports/repo-governance-audit.md`
  - `.state/workflow/reports/repo-governance-repair-report.md`
  - `.state/workflow/reports/pre-commit-dirty-tree-review.md`
  - `docs/index.md`
  - `docs/archive/README.md`
  - `docs/archive/reports/*.md`
  - `docs/archive/workflows/2026-05-14_cc-cx-integration.md`
  - `docs/reference/README.md`
  - `docs/reference/learnings/README.md`
  - `docs/reference/learnings/FEATURE_REQUESTS.md`
  - `docs/reference/learnings/LEARNINGS.md`
  - `docs/reference/policies/*.md`
  - `docs/workflows/agent-skill-inventory.md`
  - `docs/workflows/agent_tooling_baseline.md`
  - `docs/workflows/cc-cx-collaboration.md`
  - `docs/workflows/repository-layout.md`
  - `docs/workflows/work_area_registry.md`
  - `docs/workflows/worktree-policy.md`
  - `scripts/README.md`
  - `scripts/workflow/README.md`
  - `scripts/workflow/tests/cx-exec.Tests.ps1`
- Paired deletions:
  - root moved docs: `agent_tooling_baseline.md`, `work_area_registry.md`
  - docs moved policies: `docs/continuous-execution.md`, `docs/frontend-validation.md`, `docs/module-admission.md`, `docs/playwright-policy.md`, `docs/safety-boundaries.md`, `docs/task-routing.md`
  - old worktree policy: `docs/workflows/01-worktree-policy.md`
  - old archived workflow path: `docs/workflows/archives/2026-05-14_cc-cx-integration.md`
  - old test path: `tests/workflow/cx-exec.Tests.ps1`
  - root/runtime residues: `CODEX_RESULT.md`, `CLAUDE_REVIEW.md`, `.task-worktree.json`, `.workflow/reports/codex-missing-context-report.md`, `.learnings/LEARNINGS.md`
  - `.codex-exec-apple/**` as deletion-only cleanup, after group review.

## Suggested Exclusion List

Do not stage unless explicitly accepted:

- `docs/archive/learnings-retired/ERRORS.md`: raw error/log archive; inspect or keep local-only.
- Any `.state/workflow/tasks/**`, `.state/workflow/archive/**`, `.state/workflow/current/**`, `.state/workflow/state/**`, `.state/workflow/logs/**`.
- Any live `.codex-exec*`, `.workflow`, or `run/workflow` content if it reappears.
- `docs/workflows/99-pipeline-smoke-test.md` deletion until the user confirms it is obsolete.

## Blockers

- Manual decision required: whether to submit `docs/archive/learnings-retired/ERRORS.md` or keep it local/untracked.
- Manual decision required: whether to accept deletion of `docs/workflows/99-pipeline-smoke-test.md`.
- Manual group review required: `.codex-exec-apple/**` deletion removes old tracked runtime/cache from Git. This is likely desired, but it is high-volume and should not be staged blindly.

## Final Recommendation

重新跑强验收：yes. Run it after deciding the two blockers above, and before commit. The strong acceptance should verify that:

- `cx-exec.ps1 -DryRun` still writes to `.state/workflow/tasks/<task_id>/`.
- `run/workflow/`, `.workflow/`, `.codex-exec-apple/`, root `CODEX_RESULT.md`, root `CLAUDE_REVIEW.md`, root `TASK_HANDOFF.md`, and root `.task-worktree.json` are not recreated.
- CC/CX docs treat `docs/workflows/worktree-policy.md` as the canonical worktree policy.
