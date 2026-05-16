# CC / CX Orchestration

本文件只描述当前 CC -> CX 契约。历史验收、迁移记录和 proxy 细节不放 active 层。

## Roles

- CC：planning、supervision、review。
- CX：读代码、写代码、跑命令、返回结构化结果。
- Codex 独立工作：用户直接给 Codex 下任务时，不需要经过 `cx-exec.ps1`。

## Entrypoint

- 根入口：`.\cx-exec.ps1`
- executor：`scripts/workflow/cx-exec.ps1`
- wrapper：优先使用 `C:\Users\apple\codex-maintenance\codex-exec-wrapper.exe`
- `CODEX_HOME`：`C:\Users\apple\.codex-exec`
- result root：`.state/workflow/tasks/<task_id>/`

`cx-exec.ps1` 接受 `-TaskId`、`-TaskDescription`、`-Profile` 和 `-DryRun`。`-DryRun` 只写结构化 dry-run 结果，不调用真实 Codex。

## Result Contract

每个任务目录只保存机器运行态：

- `result.json`
- `codex.log`
- `codex.err.log`

`result.json` 最少表达完成项、未完成项、修改文件、验证结果、local-review 结果和下一步建议。

## Artifact Boundary

- 普通 Codex 修改任务不生成 `docs/plans/*.md`、Markdown report、probe 或 archive 证据文件。
- `.state/workflow/current/` 是滚动状态区，默认不提交。
- `.state/workflow/reports/` 只用于审查、验收、事故复盘或 commit 前人工复核。
- `docs/archive/reports/` 和 `docs/plans/` 只有用户确认需要长期留档时才使用。

## Review Boundary

CC 审查不要求根目录 `CLAUDE_REVIEW.md`。审查结论可留在 CC 输出或 `.state/workflow/reports/`；只有用户确认长期留档时，才晋升到 `docs/archive/reports/`。
