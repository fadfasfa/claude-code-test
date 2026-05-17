# CC / CX Orchestration

本文件只描述当前 CC -> CX 契约。历史验收、迁移细节和 proxy 讨论不放 active 层。

## Roles

- CC：planning、supervision、review。
- CX：读写仓库、运行命令、返回结构化结果。
- 用户直接给 Codex 下任务时，不需要经过 `cx-exec.ps1`。

## Current Contract

- 根入口：`.\cx-exec.ps1`
- executor：`scripts/workflow/cx-exec.ps1`
- wrapper：`C:\Users\apple\codex-maintenance\codex-exec-wrapper.exe`
- `CODEX_HOME`：`C:\Users\apple\.codex-exec`
- result root：`.state/workflow/tasks/<task_id>/`

`cx-exec.ps1` 支持 `-TaskId`、`-TaskDescription`、`-Profile` 和 `-DryRun`。`-DryRun` 只写结构化结果，不调用真实 Codex。

## Result Contract

每个任务目录只保留机器运行态：

- `result.json`
- `codex.log`
- `codex.err.log`

`result.json` 至少包含完成项、未完成项、修改文件、验证结果和下一步建议。

## Artifact Boundary

- 普通任务不生成 `docs/plans/*.md`、Markdown report、probe 或 archive 证据文件。
- `.state/workflow/current/` 是滚动状态区，默认不提交。
- `.state/workflow/reports/` 只用于审查、验收、事故复盘或 commit 前人工复核。
