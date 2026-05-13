# Codex Proxy Policy

## 当前原则

- Codex proxy 属于全局配置，不属于本仓业务配置。
- `codex-proxy` 本身健康时，`127.0.0.1:8080 /v1/models` 可访问；这只证明代理可用，不证明某个 Codex surface 真实命中 proxy。
- `codex-proxy` 只给已证明确实请求本地 `/v1` 或 `/v1/responses` 的 OpenAI-compatible 工具使用。
- 不能仅凭 Codex banner 中显示 `provider=codex-proxy` 判断真实命中 proxy。
- Codex App 使用 ChatGPT 账号登录，负责插件管理、App UI、cloud/thread/desktop 类能力。
- `cockpit-tools` 切换 Codex App / ChatGPT 登录态；实测共享 Codex/ChatGPT 登录态会影响 VS Code Codex 插件，VS Code 重启后可能跟随当前 cockpit 账号。
- VS Code Codex 插件当前不能验收为 `codex-proxy` 执行通道。即使通过 `code-codex-exec.ps1` 启动，实测 OpenAI Codex extension 日志仍命中 `chatgpt.com/backend-api/codex/responses`。
- VS Code Codex 插件当前应视为 ChatGPT/Codex 账号通道，而不是 proxy quota 池通道；使用时默认消耗当前 Codex/ChatGPT 账号额度。
- Codex CLI 0.121.0 的 `codex-exec.ps1` 能设置 `CODEX_HOME=C:\Users\apple\.codex-exec`，CLI banner 能显示 `provider=codex-proxy`；但 `codex exec` 实测仍请求 `chatgpt.com/backend-api/codex/responses`，不作为 proxy 验收或日常 proxy 执行入口。
- `C:\Users\apple\.codex-exec` 保留为实验/隔离配置目录。当前状态是配置隔离成功，实际 Codex CLI / VS 插件 proxy 路由未验收通过；不删除，但不再标记为 READY。
- 不把 `CODEX_HOME` 写成全局用户环境变量；profiles 只是工作模式切换。
- 日常使用口径：Codex App / VS Code Codex 走官方账号通道，受 cockpit 当前账号影响；`codex-proxy` 给真正 OpenAI-compatible 且已通过请求日志验证的工具；`claudecode` 本地 workflow 按上述边界选择执行入口。
- Codex 权限口径也属于全局配置：默认低摩擦 `granular`，高危动作由 `rules/default.rules` 拦截。
- 本仓不恢复 repo-local `.codex/config.toml`。
- 不把 API key、token、cookie、proxy secret 写入仓库。

## 可做

- 记录本仓是否依赖全局 Codex baseline。
- 在工具基线里说明本仓不维护 proxy 真相源。
- 对仓库内误写的 repo-local Codex 配置做删除或阻断。

## 不做

- 不读取或修改 `local.yaml`、`proxies.json`、账户池、真实 key。
- 不在子项目任务中改变全局代理配置。
- 不把 VS Code Codex 插件或 `codex exec` 当前状态写成已验收的 `codex-proxy` 生产执行通道。
- 不把 `full-access` profile 写成仓库默认。
