# CC / CX Orchestration

本文件只描述当前 CC -> CX 契约。历史验收、迁移细节和 proxy 讨论不放 active 层。

## Roles

- CC：planning、supervision、review。
- CX：读写仓库、运行命令、返回结构化结果。
- 用户直接给 Codex 下任务时，不需要经过 `cx-exec.ps1`。
- `cx-exec.ps1` 是 CC-led supervised mode 的委派入口，不是 Codex-led standalone mode 的必经入口。

## Current Contract

- 根入口：`.\cx-exec.ps1`
- executor：`scripts/workflow/cx-exec.ps1`
- wrapper：`C:\Users\apple\codex-maintenance\codex-exec-wrapper.exe`
- `CODEX_HOME`：`C:\Users\apple\.codex-exec`
- result root：`.state/workflow/tasks/<task_id>/`

`cx-exec.ps1` 支持 `-TaskId`、`-TaskDescription`、`-Profile`、`-Sandbox` 和 `-DryRun`。`-DryRun` 只写结构化结果，不调用真实 Codex。`-Sandbox auto` 默认按 profile 选择：`design` / `review` 使用 `read-only`，`implement` / `lint` 使用 `workspace-write`，`full-access` 使用 `danger-full-access`。

如果 Windows Codex sandbox 在当前环境报 `CreateProcessAsUserW failed: 5`，只有用户显性授权非沙箱 CX 任务时，才允许传 `-Sandbox danger-full-access`。

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

## Delegation Guard

Claude Code 通过 `.claude/settings.json` 注册 `PreToolUse` Delegation Guard。Guard 长期保护业务工作区、workflow 控制面、仓库入口文件、`.claude/settings.json`、`.claude/hooks/**` 和 `.agents/skills/**`。Guard 生效后，CC 默认不得直接修改这些路径；如需实现性修改，应通过 `.\cx-exec.ps1` 委派 CX，除非用户显性授权“允许 CC 直接修改”。

Guard 允许 CC 执行 `.\cx-exec.ps1` 或 `pwsh -File .\cx-exec.ps1` 作为委派入口；这是 supervised mode 的正常通道。直接编辑、覆盖、删除或移动 `cx-exec.ps1` 仍会被阻止。
