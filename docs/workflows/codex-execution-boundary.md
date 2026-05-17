# Codex Execution Boundary

本文件只记录 Codex 当前执行边界，不保存账号、密钥、proxy 配置或旧验收细节。

## Execution Surfaces

- Codex-led standalone mode：用户直接调用 Codex 时，Codex 按 `AGENTS.md`、`PROJECT.md`、`docs/index.md` 和用户任务独立执行普通代码任务完整流程。
- CC-led supervised mode：CC 负责 planning / supervision / review；涉及实现性修改时通过 `cx-exec.ps1` 委派 CX 执行。
- Codex App、VS Code Codex、Codex CLI、wrapper 和 CC 调用器是不同 surface，不混写为同一入口。
- `cx-exec.ps1` 是 CC 委派 Codex 的标准入口，不是 Codex 唯一入口。

## Current CX Contract

- 根入口：`.\cx-exec.ps1`
- executor：`scripts/workflow/cx-exec.ps1`
- wrapper-first：`C:\Users\apple\codex-maintenance\codex-exec-wrapper.exe`
- `CODEX_HOME`：`C:\Users\apple\.codex-exec`
- result root：`.state/workflow/tasks/<task_id>/`
- sandbox：默认 `-Sandbox auto`，按 profile 选择 `read-only` / `workspace-write` / `danger-full-access`。

## Forbidden

- 不回退到 PATH 上的 npm `codex`。
- 不重建 `.workflow/`、`.codex-exec-apple/`、`.learnings/` 或根目录 `CODEX_RESULT.md`。
- 不把 `run/workflow` 当现行 result root。
- 不恢复 repo-local `.codex/config.toml`。
- 不读取或修改 `auth.json`、token、cookie、API key、`local.yaml`、`proxies.json` 或 proxy secret。
- 不把 `full-access` profile 写成仓库默认。
- 不把 Codex-led standalone mode 改写成必须经过 `cx-exec.ps1`。
- 不在没有用户显性授权时使用 `-Sandbox danger-full-access`。

## Claude Delegation Guard

`.claude/settings.json` 注册 Claude Code `PreToolUse` guard。该 guard 只约束 CC 的直接工具调用：命中受保护路径的 `Edit`、`Write`、`MultiEdit` 会被阻止；命中受保护路径的修改型 `Bash` 会被阻止；只读和验证类 Bash 保持允许。

## Related

- `10-cc-cx-orchestration.md`
- `repository-layout.md`
- `worktree-policy.md`
