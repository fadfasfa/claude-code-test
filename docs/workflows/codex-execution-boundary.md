# Codex Execution Boundary

本文件只记录 Codex 当前执行边界，不保存账号池细节、密钥信息或旧验收记录。

## Execution Surfaces

- Codex 独立工作：按 `AGENTS.md`、`PROJECT.md`、`docs/index.md` 和用户任务执行。
- CC -> CX 调用：CC 负责 planning / supervision / review，CX 通过 `cx-exec.ps1` 执行。
- Codex App：使用 ChatGPT 登录，负责 App UI、插件、cloud/thread/desktop 类能力。
- VS Code Codex、Codex CLI、wrapper 和 CC 调用器是不同 surface，不得混写为同一入口。

## Proxy Boundary

- 本仓只记录执行边界，不维护 proxy 配置、账号池、usage dashboard 或 quota 结论。
- `codex-proxy` 只给已证明确实请求本地 `/v1` 或 `/v1/responses` 的 OpenAI-compatible 工具使用。
- 健康检查只能证明本地代理可访问，不能证明某个 Codex surface 实际命中 proxy。

## CC -> CX Contract

- 根入口：`.\cx-exec.ps1`
- executor：`scripts/workflow/cx-exec.ps1`
- wrapper-first：`C:\Users\apple\codex-maintenance\codex-exec-wrapper.exe`
- `CODEX_HOME`：`C:\Users\apple\.codex-exec`
- result root：`.state/workflow/tasks/<task_id>/`

## Forbidden

- 不回退到 PATH 上的 npm `codex`。
- 不重建 `.workflow/`、`.codex-exec-apple/` 或根目录 `CODEX_RESULT.md`。
- 不把 `run/workflow` 当现行 result root。
- 不恢复 repo-local `.codex/config.toml`。
- 不读取或修改 `auth.json`、token、cookie、API key、`local.yaml`、`proxies.json` 或 proxy secret。
- 不把 `full-access` profile 写成仓库默认。

## Related

- `docs/workflows/10-cc-cx-orchestration.md`
- `docs/workflows/repository-layout.md`
- `docs/workflows/worktree-policy.md`
