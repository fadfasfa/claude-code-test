# Model Routing

## 默认路由

- Codex 是本仓当前唯一主执行入口。
- Claude Code 只保留白板和必要接口，不作为本仓治理面。
- 本仓不设置 repo-local `.codex/config.toml`。
- 全局根层 `model_provider = "codex-proxy"` 是默认 provider；profiles 只切换工作模式，不承担默认 provider 修复职责。
- Codex App 使用 ChatGPT 账号登录，只负责插件管理、App UI、cloud/thread/desktop 类能力；API key / `codex-proxy` 登录下如果插件或 cloud 能力不可用，不再尝试强行绕过。
- VS Code Codex 插件和 Codex CLI 作为本地执行主力，固定通过全局 `codex-proxy` provider 使用 `CODEX_PROXY_API_KEY`，不跟随 Codex App 当前 ChatGPT 登录账号切换。
- `cockpit-tools` 只允许切换 Codex App / ChatGPT 登录态，不修改 VS Code Codex 插件配置、全局 `codex-proxy` provider、`CODEX_PROXY_API_KEY`、VS 插件认证状态或 CLI 默认执行链路。
- 日常代码额度通过 `codex-proxy` 账号池消耗；Codex App 的 ChatGPT 账号额度只用于插件、云端、桌面和 App 专属能力。
- 权限配置走全局默认：低摩擦 `granular` + `rules/default.rules` 高危闸门。
- `full-access` profile 只能人工选择，不是本仓默认执行面。

## Skill 路由

- 代码任务触发 `karpathy-project-bridge` 和全局 `karpathy-guidelines`。
- 前端 UI 任务触发 `frontend-design-project-bridge` 和全局 `frontend-design`，同时仍触发 `karpathy-project-bridge`。
- Superpowers 只作为能力索引，不覆盖仓库规则。

## 禁止

- 不在本仓写入真实 API key、token、cookie 或 proxy secret。
- 不用子项目任务修改全局模型、代理或账户配置。
- 不让 VS Code Codex 插件依赖 Codex App 当前 ChatGPT 登录态。
- 不为本仓恢复 repo-local Codex config。
