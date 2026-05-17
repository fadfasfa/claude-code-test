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

如果 Windows Codex sandbox 在当前环境报 `CreateProcessAsUserW failed: 5`，只有用户显性授权非沙箱 CX 任务时，才允许传 `-Sandbox danger-full-access`。这是 CC -> CX 委派的授权，不等于允许 CC 直接修改受保护文件。

## Permission Baseline

本仓 `.claude/settings.json` 采用“全局可读、本仓沙箱内可写、其余显式确认”的低摩擦口径：

- `permissions.defaultMode=acceptEdits`。
- `permissions.allow` 包含 `Read`、`Edit(/**)`、`Write(/**)` 和 `MultiEdit(/**)`。
- `permissions.ask` 包含 `Bash`，但 `sandbox.autoAllowBashIfSandboxed=true` 时沙箱内 Bash 可自动执行。
- `sandbox.enabled=true`，默认写入边界是本仓工作目录；沙箱不可用或命令需要非沙箱时仍回到显式确认。
- 凭据类文件仍通过 `permissions.deny` 和仓库规则禁止读取。
- Delegation Guard 的 deny 决策优先级高于这些 allow 规则，仍保护业务区和控制面。

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

Claude Code 通过 `.claude/settings.json` 注册 `PreToolUse` Delegation Guard。Guard 长期保护业务工作区、workflow 控制面、仓库入口文件、`.claude/settings.json`、`.claude/hooks/**` 和 `.agents/skills/**`。Guard 生效后，CC 默认不得直接修改这些路径；如需实现性修改，应通过 `.\cx-exec.ps1` 委派 CX。

Guard 允许 CC 执行 `.\cx-exec.ps1`、`pwsh -File .\cx-exec.ps1` 或 `pwsh -Command "& .\cx-exec.ps1 ..."` 作为委派入口；这是 supervised mode 的正常通道。直接编辑、覆盖、删除或移动 `cx-exec.ps1` 仍会被阻止。

用户显性授权“允许 CC 直接修改”时，还必须有一个机器可判定信号，guard 才会放行 CC 对受保护路径的直接修改：

- Claude Code hook 输入为 `permission_mode=bypassPermissions`。
- 启动 Claude Code 的进程环境设置了 `CC_CX_ALLOW_DIRECT_MODIFICATION=1`。

没有上述信号时，guard 仍返回 deny，并提示改用 `.\cx-exec.ps1` 委派 CX。
