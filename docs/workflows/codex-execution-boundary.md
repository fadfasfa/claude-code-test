# Codex Execution Boundary

本文件是 Codex 执行面、proxy 口径和 CC -> CX 调用边界的当前规则源。它只保留现行可执行边界，不保存旧验收记录、账号池细节或密钥信息。

## Execution Surfaces

- Codex 独立工作模式：用户直接给 Codex 下任务时，Codex 按 `AGENTS.md`、`PROJECT.md`、`docs/index.md` 和任务上下文执行，不需要经过 `cx-exec.ps1`。
- CC -> CX 调用模式：Claude Code 负责 planning、supervision 和 review；Codex 作为执行器读代码、写代码、跑命令并输出结构化结果。
- Codex App 继续使用 ChatGPT 账号登录，负责插件管理、App UI、cloud/thread/desktop 类能力。
- VS Code Codex 插件当前按 ChatGPT/Codex 账号通道处理；是否真实命中 proxy 只能由请求路径或日志验证，不能靠界面或 banner 推断。
- Codex CLI / wrapper / CC 调用器是不同 surface，不得混写为同一个执行入口。

## Proxy And Quota Boundary

- `codex-proxy` 是 Codex 执行层的接入方式，不是 Claude Code 主脑，也不改变 CC 的 planning / supervision 角色。
- `codex-proxy` 只给已证明确实请求本地 `/v1` 或 `/v1/responses` 的 OpenAI-compatible 工具使用。
- 本仓只记录执行边界，不维护 proxy 配置、账号池、真实 key、usage dashboard 或 quota 结论。
- 当前可验证口径：proxy 健康检查只能证明本地代理可访问，不能证明某个 Codex surface 实际命中 proxy。
- 额度与路由只写已确认事实；无法从现有文件确认的 dashboard / usage 结论保持未确认，不写成当前规则。

## CC -> CX Contract

- 根入口：`.\cx-exec.ps1`。
- executor：`scripts/workflow/cx-exec.ps1`。
- 启动顺序：wrapper-first，优先使用 `C:\Users\apple\codex-maintenance\codex-exec-wrapper.exe`。
- `CODEX_HOME`：`C:\Users\apple\.codex-exec`。
- result root：`.state/workflow/tasks/<task_id>/`。
- stdout / stderr 由 executor 写入 task 目录，长期验收报告进入 `.state/workflow/reports/` 或 `docs/archive/reports/`。

## Forbidden

- 不回退到 PATH 上的 npm `codex`。
- 不重建 `.workflow/`。
- 不重建 `.codex-exec-apple/`。
- 不写根目录 `CODEX_RESULT.md`。
- 不把 `run/workflow` 当现行 result root。
- 不恢复 repo-local `.codex/config.toml`。
- 不读取或修改 `auth.json`、token、cookie、API key、`local.yaml`、`proxies.json` 或 proxy secret。
- 不把 `full-access` profile 写成仓库默认。

## Related Documents

- `docs/workflows/10-cc-cx-orchestration.md`
- `docs/workflows/cc-cx-collaboration.md`
- `docs/workflows/repository-layout.md`
- `docs/workflows/worktree-policy.md`
- `docs/workflows/agent_tooling_baseline.md`
