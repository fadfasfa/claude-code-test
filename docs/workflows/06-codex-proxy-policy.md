# Codex Proxy Policy

## 当前原则

- Codex proxy 属于全局配置，不属于本仓业务配置。
- 全局根层 `model_provider = "codex-proxy"` 是 VS Code Codex 插件和 Codex CLI 的默认 provider 来源；profiles 只是工作模式切换。
- VS Code Codex 插件和 Codex CLI 是本地执行主力，固定通过全局 `codex-proxy` provider 使用 `CODEX_PROXY_API_KEY`。
- Codex App 使用 ChatGPT 账号登录，负责插件管理、App UI、cloud/thread/desktop 类能力；API key / `codex-proxy` 登录下如果插件或 cloud 能力不可用，不再尝试强行绕过。
- VS Code Codex 插件和 Codex CLI 不跟随 Codex App 当前 ChatGPT 登录账号切换。
- `cockpit-tools` 只允许切换 Codex App / ChatGPT 登录态，不修改 VS Code Codex 插件配置、全局 `codex-proxy` provider、`CODEX_PROXY_API_KEY`、VS 插件认证状态或 CLI 的 `codex-proxy` 默认执行链路。
- 日常代码额度通过 `codex-proxy` 账号池消耗；Codex App 的 ChatGPT 账号额度只用于插件、云端、桌面和 App 专属能力。
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
- 不把 Codex App 登录态作为 VS Code Codex 插件或 CLI 的认证真相。
- 不把 `full-access` profile 写成仓库默认。
