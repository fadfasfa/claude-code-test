# Codex Execution Boundary

本文件只记录 Codex 当前执行边界，不保存账号、密钥、proxy 配置或旧验收细节。

## Execution Surfaces

- Codex 独立工作：按 `AGENTS.md`、`PROJECT.md`、`docs/index.md` 和用户任务执行。
- CC -> CX：CC 负责 planning / supervision / review，CX 通过 `cx-exec.ps1` 执行。
- Codex App、VS Code Codex、Codex CLI、wrapper 和 CC 调用器是不同 surface，不混写为同一入口。

## Current CX Contract

- 根入口：`.\cx-exec.ps1`
- executor：`scripts/workflow/cx-exec.ps1`
- wrapper-first：`C:\Users\apple\codex-maintenance\codex-exec-wrapper.exe`
- `CODEX_HOME`：`C:\Users\apple\.codex-exec`
- result root：`.state/workflow/tasks/<task_id>/`

## Forbidden

- 不回退到 PATH 上的 npm `codex`。
- 不重建 `.workflow/`、`.codex-exec-apple/`、`.learnings/` 或根目录 `CODEX_RESULT.md`。
- 不把 `run/workflow` 当现行 result root。
- 不恢复 repo-local `.codex/config.toml`。
- 不读取或修改 `auth.json`、token、cookie、API key、`local.yaml`、`proxies.json` 或 proxy secret。
- 不把 `full-access` profile 写成仓库默认。

## Related

- `10-cc-cx-orchestration.md`
- `repository-layout.md`
- `worktree-policy.md`
