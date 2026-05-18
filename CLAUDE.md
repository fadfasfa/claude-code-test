# Claude Code Entry

本文件是 `claudecode` 仓库的 Claude Code 入口。Codex 规则以 `AGENTS.md` 为准。

## Role

- CC / Claude Code：大脑、监工、审计、验收；负责理解目标、收敛计划、监督过程、审查 diff 和验收结果。
- CX / Codex：手、眼、实现者、大范围探查者、验证者；负责复杂探查、实现、运行验证和第二意见。
- Codex-led standalone mode：用户直接调用 Codex 时，Codex 可按 `AGENTS.md`、`docs/index.md` 和用户任务独立完成普通代码任务。
- CC-led supervised mode：CC 负责监督和验收；需要调用 CX 时，后续主路是 OpenAI 官方 Codex plugin。

## CX Boundary

- 默认使用简体中文。
- 本仓 `.claude/settings.json` 已启用 OpenAI 官方 Codex plugin，作为 CC 调用 CX 的默认主路。
- plugin 启用不等于 review gate 启用；review gate 默认禁用，除非用户显性要求，否则不得启用。
- 不再维护 `cx-exec` 作为 fallback 或 legacy 主路。
- `.state/workflow/**` 是旧 `cx-exec` 工作流遗留运行态目录；新 CC-CX 主路不再依赖 `.state/workflow/tasks/result.json`。
- Codex standalone 能力保留：用户直接调用 Codex 时，Codex 仍可独立完成普通代码任务、运行命令和验证结果。
- 普通任务不生成计划、Markdown report、probe 或 archive 证据文件。

## Permission Baseline

- `.claude/settings.json` 使用 `acceptEdits`，让本仓内普通 `Edit` / `Write` / `MultiEdit` 低摩擦执行。
- `Read` 默认允许，以减少只读探索提示；凭据类文件仍通过 deny 规则和仓库规则禁止读取。
- `Bash(*)` 默认允许；普通仓库内 Bash 不再逐条确认，仍由 sandbox 和仓库规则兜底。
- 本仓外写入、非沙箱命令以及高风险、不可逆或发布类 Bash 操作仍进入显式确认。

## Bash Risk Guard

- `.claude/settings.json` 仍注册 `PreToolUse` guard，但现在只用于高风险 Bash 的显式确认。
- Guard 不再拦截普通 `Edit`、`Write`、`MultiEdit`，也不再因为命中受保护路径就一律阻断普通 Bash。
- 当前 guard 重点覆盖删除文件、强制清理、回退工作树、重写 Git 历史、提交和推送等较难回滚操作。
- 本仓外写入或非沙箱执行仍由 Claude Code 自身权限和 sandbox 边界继续提示。
- Codex plugin review gate 默认禁用；除非用户显性要求，不得启用 review gate。
- 已安装 skills 是独立能力资产；CC-CX 工作流清理不等于清理 skills。

CC 如需让 CX 使用 worktree，必须在上游任务中显式写明 `requires_worktree: true` 并等待用户确认。
