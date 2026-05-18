# CC / CX Orchestration

本文件只描述当前 CC -> CX 契约。历史验收、迁移细节和 proxy 讨论不放 active 层。

## Roles

- CC / Claude Code：大脑、监工、审计、验收；负责理解目标、收敛计划、监督过程、审查 diff 和验收结果。
- CX / Codex：手、眼、实现者、大范围探查者、验证者；负责复杂探查、实现、运行验证和第二意见。
- 用户直接给 Codex 下任务时，Codex 保留 standalone 能力，可按 `AGENTS.md`、`docs/index.md` 和用户任务独立完成普通代码任务。
- Claude Code 入口下，CC 需要调用 CX 时，后续主路是 OpenAI 官方 Codex plugin。

## Current Contract

- `.claude/settings.json` 已启用 OpenAI 官方 Codex plugin，作为 CC 调用 CX 的默认主路。
- plugin 启用不等于 review gate 启用；review gate 默认禁用，除非用户显性要求，否则不得启用。
- 不再维护 `cx-exec` 作为 fallback 或 legacy 主路。
- 旧 `scripts/workflow/` 执行器和根 `cx-exec.ps1` 已移除。
- `.state/workflow/**` 是旧 `cx-exec` 工作流遗留运行态目录；新 CC-CX 主路不再依赖 `.state/workflow/tasks/result.json`、`codex.log` 或 `codex.err.log` 作为默认验收接口。

## Permission Baseline

本仓 `.claude/settings.json` 采用“全局可读、本仓沙箱内可写、其余显式确认”的低摩擦口径：

- `permissions.defaultMode=acceptEdits`。
- `permissions.allow` 包含 `Read`、`Edit(/**)`、`Write(/**)` 和 `MultiEdit(/**)`。
- `permissions.ask` 包含 `Bash`，但 `sandbox.autoAllowBashIfSandboxed=true` 时沙箱内 Bash 可自动执行。
- `sandbox.enabled=true`，默认写入边界是本仓工作目录；沙箱不可用或命令需要非沙箱时仍回到显式确认。
- 凭据类文件仍通过 `permissions.deny` 和仓库规则禁止读取。
- Delegation Guard 的 deny 决策优先级高于这些 allow 规则，仍保护业务区和控制面。

## Result Contract

新主路不再使用旧 `cx-exec` 的固定 `result.json` 契约。CC 验收以实际 diff、命令输出、测试结果和 Codex plugin 返回内容为准；`.state/workflow/tasks/**` 仅保留为旧运行态遗留。

## Artifact Boundary

- 普通任务不生成 `docs/plans/*.md`、Markdown report、probe 或 archive 证据文件。
- `.state/cc-work/**` 可用于 CC 计划、协作草稿、交接稿和审查草稿；不是正式文档区。
- `.state/workflow/current/`、`.state/workflow/reports/`、`.state/workflow/logs/` 和 `.state/workflow/tasks/` 都是旧流程遗留，不是 active workflow 默认目录。

## Delegation Guard

Claude Code 通过 `.claude/settings.json` 注册 `PreToolUse` Delegation Guard。Guard 长期保护业务工作区和控制面。Guard 生效后，CC 默认不得直接修改这些路径；实现、修复、批量修改、复杂探查和运行验证默认应交给 CX / Codex plugin。

默认 block：

- `Edit` / `Write` / `MultiEdit` 命中受保护业务区。
- `Edit` / `Write` / `MultiEdit` 命中控制面。
- `Bash` 修改受保护业务区。
- `Bash` 修改控制面。

默认 allow：

- `Read` / `Glob` / `Grep` / `LS`。
- `git status` / `git diff` / `git log`。
- 只读 Bash 命令和测试命令。
- `.state/cc-work/**` 下的计划、协作、交接和审查草稿写入。

用户显性要求“允许 CC 直接修改”“允许 Claude 直接修改”“这次 CC 直接写”“显性授权 CC 写入”时，文档上允许 CC 直写。guard 不解析用户原文；显性授权场景需要通过当前 Claude Code 权限确认或人工允许完成。

阻断提示固定使用中文，核心语义是：Claude Code 是监工和审计者，不是默认实现者；非显性授权下，不允许 CC 直接修改受保护业务区或控制面文件；请将实现、复杂探查或验证委派给 CX / Codex plugin；计划和协作草稿请写入 `.state/cc-work/**`。

## Skills

已安装 skills 是独立能力资产。本次 CC-CX 工作流清理不移动、删除或重写 `.agents/skills/**` 或 `.claude/skills/**`；工作流清理不等于清理 skills。
