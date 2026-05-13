# Model Routing

## 默认路由

- Codex 是本仓当前唯一主执行入口。
- Claude Code 只保留白板和必要接口，不作为本仓治理面。
- 本仓不设置 repo-local `.codex/config.toml`。
- Codex App 使用 ChatGPT 账号登录，负责插件管理、App UI、cloud/thread/desktop 类能力。
- `cockpit-tools` 切换 Codex App / ChatGPT 登录态；当前已观测到共享登录态会影响 VS Code Codex 插件，VS Code 重启后可能跟随当前 cockpit 账号。
- VS Code Codex 插件当前不是已验收的 `codex-proxy` 执行通道。即使通过 `code-codex-exec.ps1` 启动，实测 OpenAI Codex extension 日志仍命中 `chatgpt.com/backend-api/codex/responses`。
- 使用 VS Code Codex 时，按当前 Codex/ChatGPT 账号通道处理，默认消耗当前账号额度。
- Codex CLI 0.121.0 可通过 `codex-exec.ps1` 设置 `CODEX_HOME=C:\Users\apple\.codex-exec`，且 banner 可显示 `provider=codex-proxy`；但 `codex exec` 实测仍请求 `chatgpt.com/backend-api/codex/responses`，不作为 proxy 验收或日常 proxy 执行入口。
- `C:\Users\apple\.codex-exec` 保留为实验/隔离配置目录；当前状态是配置隔离成功，实际 CLI / VS 插件 proxy 路由未验收通过，不再标记为 READY。
- `codex-proxy` 只给已证明确实请求本地 `/v1` 的 OpenAI-compatible 工具使用；不能仅凭 Codex banner 中显示 `provider=codex-proxy` 判断真实命中 proxy。
- 日常路由口径：Codex App / VS Code Codex 走官方账号通道；`codex-proxy` 给真实命中本地 `/v1` 且已通过请求日志验证的工具；`claudecode` 本地 workflow 仍按上述边界选择入口。
- 不把 `CODEX_HOME` 写成全局用户环境变量；profiles 只切换工作模式，不承担默认 provider 修复职责。
- 权限配置走全局默认：低摩擦 `granular` + `rules/default.rules` 高危闸门。
- `full-access` profile 只能人工选择，不是本仓默认执行面。

## Skill 路由

- 代码任务触发 `karpathy-project-bridge` 和全局 `karpathy-guidelines`。
- 前端 UI 任务触发 `frontend-design-project-bridge` 和全局 `frontend-design`，同时仍触发 `karpathy-project-bridge`。
- Superpowers 只作为能力索引，不覆盖仓库规则。

## 禁止

- 不在本仓写入真实 API key、token、cookie 或 proxy secret。
- 不用子项目任务修改全局模型、代理或账户配置。
- 不把 VS Code Codex 插件当前状态写成已验收的 `codex-proxy` 生产执行通道。
- 不为本仓恢复 repo-local Codex config。
