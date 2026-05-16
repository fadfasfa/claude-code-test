# Root Simplification Report

## 1. Completed

- Root simplified: yes.
- No commit, push, branch creation, or staging was performed.
- CC -> CX entrypoints remain:
  - `cx-exec.ps1`
  - `scripts/workflow/cx-exec.ps1`
- VS Code wrapper was not modified:
  - `C:\Users\apple\codex-maintenance\codex-exec-wrapper.exe`

## 2. Root Before / After

Before cleanup, root included these disputed items:

- `.codex-exec-apple`
- `.workflow`
- `tests/workflow`
- `.learnings`
- `agent_tooling_baseline.md`
- `work_area_registry.md`
- tracked root runtime files already marked deleted: `CODEX_RESULT.md`, `CLAUDE_REVIEW.md`, `.task-worktree.json`

After cleanup, root-level items are:

```text
.agents
.claude
.codex
.git
.githooks
docs
heybox
qm-run-demo
QuantProject
run
scripts
sm2-randomizer
subtitle_extractor
.gitignore
AGENTS.md
CLAUDE.md
cx-exec.ps1
PROJECT.md
README.md
```

The five independent repo directories were not moved.

## 3. Disputed Paths

- `.codex-exec-apple`: moved to `run/workflow/archive/obsolete-codex-exec-apple-20260515-224617/`; root removed.
- `.workflow`: merged into `run/workflow/`; root removed.
- `tests/workflow`: moved to `scripts/workflow/tests/`; root `tests/` removed because it only contained workflow tests.
- `.learnings`: moved to `docs/learnings/`; root removed.
- `agent_tooling_baseline.md`: moved to `docs/workflows/agent_tooling_baseline.md`.
- `work_area_registry.md`: moved to `docs/workflows/work_area_registry.md`.
- `docs/workflows/99-pipeline-smoke-test.md`: deleted as old mock/smoke artifact.
- Root temporary files: absent after cleanup.

## 4. Documentation Updates

- `README.md`: now has a `Repository map` table covering root files/directories, audience, edit/delete guidance, and typical commands.
- `PROJECT.md`: now points registry/baseline to `docs/workflows/` and describes `run/workflow/`.
- `AGENTS.md`, `.agents/skills/*`, `docs/task-routing.md`, `docs/safety-boundaries.md`, and workflow docs now reference `docs/workflows/work_area_registry.md`.
- `docs/workflows/repository-layout.md`: added as the canonical directory layout explanation.
- `docs/workflows/cc-cx-collaboration.md` and `docs/workflows/10-cc-cx-orchestration.md`: updated to use `run/workflow/tasks/<task_id>/`.

## 5. DryRun Result

Command:

```powershell
.\cx-exec.ps1 -TaskId "layout-dryrun" -TaskDescription "Verify layout cleanup dry run only" -Profile implement -DryRun
```

Result:

- Exit code: 0
- Result file: `run/workflow/tasks/layout-dryrun/result.json`
- Codex invoked: no
- Root `CODEX_RESULT.md` generated: no
- Root `.workflow` recreated: no
- Root `.codex-exec-apple` recreated: no

## 6. Git Status

Final status is intentionally dirty and uncommitted.

High-level groups:

- Modified docs/scripts:
  - `.gitignore`
  - `AGENTS.md`
  - `CLAUDE.md`
  - `PROJECT.md`
  - `README.md`
  - `.agents/skills/*.md`
  - `docs/**/*.md`
  - `scripts/workflow/cx-exec.ps1`
- Moved governance docs:
  - `agent_tooling_baseline.md` -> `docs/workflows/agent_tooling_baseline.md`
  - `work_area_registry.md` -> `docs/workflows/work_area_registry.md`
- Moved learnings:
  - `.learnings/LEARNINGS.md` -> `docs/learnings/LEARNINGS.md`
  - ignored `.learnings/ERRORS.md` and `.learnings/FEATURE_REQUESTS.md` -> `docs/learnings/`
- Moved reports:
  - `.workflow/reports/*` -> `run/workflow/reports/`
- Moved tests:
  - `tests/workflow/cx-exec.Tests.ps1` -> `scripts/workflow/tests/cx-exec.Tests.ps1`
- Removed mock artifact:
  - `docs/workflows/99-pipeline-smoke-test.md`
- Removed root runtime residue:
  - `.codex-exec-apple/**`
  - `.task-worktree.json`
  - `CLAUDE_REVIEW.md`
  - `CODEX_RESULT.md`

## 7. Remaining Risks

- `.codex-exec-apple/**` was tracked in Git, so the cleanup appears as many deletions. This is expected but should be reviewed by CC before any commit.
- `run/workflow/archive/` is ignored; archived old runtime state is local evidence, not a tracked artifact.
- Historical reports under `run/workflow/reports/` may mention `.workflow` as prior state; active scripts and docs now use `run/workflow`.
- No full real Codex run was executed in this cleanup phase; only required dry-run was executed.

## 8. CC Full-flow Verification Instruction

```powershell
$ccPrompt = @'
你现在在 C:\Users\apple\claudecode 仓库中做最终验收，不要 commit，不要 push，不要创建分支。

请按 CC = 大脑/监工/最终验收者，CX = 手和眼睛 的设计做一次完整流程验证：

1. 读取并理解这些文件：
   - README.md
   - PROJECT.md
   - AGENTS.md
   - docs/workflows/repository-layout.md
   - docs/workflows/cc-cx-collaboration.md

2. 确认根目录不再存在这些路径：
   - .workflow
   - .codex-exec-apple
   - tests/workflow
   - .learnings
   - CODEX_RESULT.md
   - CLAUDE_REVIEW.md
   - TASK_HANDOFF.md
   - diagnosis.log
   - .task-worktree.json

3. 生成一个小任务并通过 CX 执行。任务必须无害，只允许读取文件，不允许修改业务文件。请运行：
   .\cx-exec.ps1 -TaskId "cc-full-flow-check" -TaskDescription "Read README.md, PROJECT.md, AGENTS.md, docs/workflows/repository-layout.md, and docs/workflows/cc-cx-collaboration.md. Report whether the repository layout says CC is supervisor, CX is executor, and runtime results go to run/workflow/tasks. Do not modify files." -Profile review

4. 读取：
   - run/workflow/tasks/cc-full-flow-check/result.json
   - run/workflow/tasks/cc-full-flow-check/codex.log
   - git status --short --untracked-files=all

5. 判断是否符合：
   - CC 负责规划、监督、最终验收。
   - CX 负责读代码、写代码、跑命令、产出结构化结果。
   - Codex 独立工作不依赖 cx-exec.ps1。
   - CC 调用 CX 时走 .\cx-exec.ps1 -> scripts/workflow/cx-exec.ps1 -> C:\Users\apple\codex-maintenance\codex-exec-wrapper.exe。
   - CX 结果写入 run/workflow/tasks/<task_id>/result.json。
   - 根目录没有运行态垃圾。

6. 输出验收结论：
   - PASS 或 BLOCK
   - 如果 BLOCK，列出必须修复项
   - 如果 PASS，说明可以进入最终痕迹清理和 commit 准备

不要 commit。不要 push。不要删除文件。不要读取或打印任何 token、auth.json、cookie 或完整 API key。
'@
claude -p $ccPrompt
```

## 9. Recommendation

- 建议进入最终收口阶段：not yet.
- 先让 CC 执行上面的全流程验证指令。
- 只有 CC 验证 PASS 后，才进入最终痕迹清理、commit 和 push 决策。
