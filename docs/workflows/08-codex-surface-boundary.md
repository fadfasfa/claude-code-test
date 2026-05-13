# Codex Surface Boundary

本文件记录 Codex App、VS Code Codex、Codex CLI 和 `codex-proxy` 的当前实测边界。它只描述已验收事实，不维护密钥、账号池或代理配置。

## 当前结论

- Codex App 继续使用 ChatGPT 账号登录，负责插件管理、App UI、cloud/thread/desktop 类能力。
- `cockpit-tools` 用于切换 Codex App / ChatGPT 账号；实测显示共享 Codex/ChatGPT 登录态会影响 VS Code Codex 插件，VS Code 重启后可能跟随当前 cockpit 账号。
- VS Code Codex 插件当前不能验收为 `codex-proxy` 执行通道。即使通过 `code-codex-exec.ps1` 启动，实测 OpenAI Codex extension 日志仍命中 `chatgpt.com/backend-api/codex/responses`。
- VS Code Codex 插件当前应视为 ChatGPT/Codex 账号通道，而不是 proxy quota 池通道；使用 VS Code Codex 时默认消耗当前 Codex/ChatGPT 账号额度。
- Codex CLI 0.121.0 的 `codex-exec.ps1` 能设置 `CODEX_HOME=C:\Users\apple\.codex-exec`，CLI banner 能显示 `provider=codex-proxy`；但 `codex exec` 实测仍请求 `chatgpt.com/backend-api/codex/responses`。
- `codex exec` 当前不作为 proxy 验收或日常 proxy 执行入口。
- `C:\Users\apple\.codex-exec` 保留为实验/隔离配置目录。当前状态是配置隔离成功，实际 Codex CLI / VS 插件 proxy 路由未验收通过；不删除，后续可用于新版 Codex CLI 或其他实验，但不再标记为 READY。
- `codex-proxy` 本身健康时，`127.0.0.1:8080 /v1/models` 可访问；但只能给已证明确实请求本地 `/v1` 的工具使用。
- 不能仅凭 Codex banner 中显示 `provider=codex-proxy` 判断真实命中 proxy。

## 日常使用口径

- Codex App / VS Code Codex：官方账号通道，受 cockpit 当前账号影响。
- `codex-proxy`：给真正 OpenAI-compatible 且已通过请求日志验证的工具。
- `claudecode` 本地 workflow 仍可用；执行入口按上述边界选择。

