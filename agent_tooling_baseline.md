# Agent 工具基线

本文件记录 `claudecode` 仓库内工具基线，不是全局事实源。

Codex 是当前唯一主流程。Claude Code 只保留白板和必要接口，不作为本仓治理面。

## 范围

- 仓库：`C:\Users\apple\claudecode`
- 工作区注册表：`work_area_registry.md`
- 仓库级 skill 白名单：`.agents/skills/README.md`
- workflow 文档：`docs/workflows/`
- workflow 脚本：`scripts/workflow/`
- 全局工具 inventory 在仓库外维护，不在这里重复。

## 当前全局 Codex baseline

- Codex App 继续使用 ChatGPT 账号登录，负责插件管理、App UI、cloud/thread/desktop 类能力。
- `cockpit-tools` 切换 Codex App / ChatGPT 账号；实测显示共享 Codex/ChatGPT 登录态会影响 VS Code Codex 插件，VS Code 重启后可能跟随当前 cockpit 账号。
- 默认 `C:\Users\apple\.codex` 保留给 Codex App 与 `cockpit-tools` 使用，用于 ChatGPT 登录、插件、App UI、cloud/thread/desktop 类能力；Codex App 启动可能重写默认 `config.toml`，这不等同于 `.codex-exec` 失效。
- `C:\Users\apple\.codex-exec` 保留为实验/隔离配置目录。当前结论是配置隔离成功，但 VS Code Codex 插件与 Codex CLI 的真实 proxy 路由未验收通过，不再标记为 READY。
- VS Code Codex 插件当前不能验收为 `codex-proxy` 执行通道。即使通过 `code-codex-exec.ps1` 启动，实测 OpenAI Codex extension 日志仍命中 `chatgpt.com/backend-api/codex/responses`。
- VS Code Codex 插件当前按 ChatGPT/Codex 账号通道处理，默认消耗当前 Codex/ChatGPT 账号额度。
- Codex CLI 0.121.0 的 `codex-exec.ps1` 能设置 `CODEX_HOME=C:\Users\apple\.codex-exec`，CLI banner 能显示 `provider=codex-proxy`；但 `codex exec` 实测仍请求 `chatgpt.com/backend-api/codex/responses`，不作为 proxy 验收或日常 proxy 执行入口。
- `codex-proxy` 本身健康检查可用时，只说明本地代理存在；只能给已证明确实请求本地 `/v1` 的工具使用。
- 不能仅凭 Codex banner 中显示 `provider=codex-proxy` 判断真实命中 proxy。
- 不把 `CODEX_HOME` 写成全局用户环境变量；只在 `C:\Users\apple\_codex_launchers\*.ps1` 当前进程内设置。
- `cockpit-tools` 不修改 VS Code Codex 插件配置、全局 `codex-proxy` provider、`CODEX_PROXY_API_KEY` 或 VS 插件认证状态。
- 日常使用口径：Codex App / VS Code Codex 走官方账号通道，受当前 Codex/ChatGPT 登录态影响；`codex-proxy` 给真正 OpenAI-compatible 且已通过请求日志验证的工具。
- 默认权限口径是低摩擦 `granular`：普通工作区读写、脚本、lint、test、build 和已启用网络不频繁询问。
- `rules/default.rules` 是高危动作闸门；发布、破坏性 Git、删除、敏感边界必须 prompt 或 forbidden。
- `full-access` profile 只能人工选择，不是任何仓库的默认权限口径。
- 本仓不再设置 repo-local `.codex/config.toml`，也不得恢复该文件作为默认项目配置。
- `frontend-design` 全局 skill 已可见；本仓只保留 `frontend-design-project-bridge` 作为触发边界说明。
- 全局 `karpathy-guidelines` 可由 `karpathy-project-bridge` 桥接。
- Superpowers 可用，但只作为能力索引，不覆盖本仓规则。

## 边界

- 普通仓库任务不得修改全局 Claude Code、Codex、Superpowers、ECC、CLI、VS plugin、Codex App 或代理配置。
- 不用 `cockpit-tools` 修改 VS Code Codex 插件配置、全局 `codex-proxy` provider、`CODEX_PROXY_API_KEY` 或 VS 插件认证状态。
- 不修改 `C:\Users\apple\.claude`、`C:\Users\apple\.codex`、全局 hooks、全局 skills、全局 AGENTS / CLAUDE 文件或 KB 仓库，除非用户明确纳入范围。
- 不触碰凭据、auth 文件、token、cookie、API key、proxy secret 或私有配置。
- 任何备份失败都必须立即停止，不得继续执行删除、覆盖、移动或其他破坏性动作。

## 当前仓库级 Skill

仓库级 Codex skill 只以 `.agents/skills/README.md` 白名单为准：

- `repo-module-admission`
- `repo-verification-before-completion`
- `repo-local-pr-review`
- `repo-maintenance`
- `karpathy-project-bridge`
- `superpowers-project-bridge`
- `frontend-design-project-bridge`

不新增 memory、learning promotion、长期上下文晋升、自动 PR shipping、高权限 worktree governance 或 task resume skill。

## 当前工具默认

- Windows 默认 shell 是 PowerShell。
- Git 操作默认只读。
- 不设置项目级 `.codex/config.toml`。
- 不设置 repo-local Codex hook。
- 不设置 repo-local MCP 配置。
- Playwright 只作为任务级可选前端验证工具；不经准入和用户确认，不安装依赖或新增配置。
- `scripts/workflow/finalize-pr.ps1` 默认只做 dry-run。

## 验证基线

完成前报告：

- 修改文件。
- 是否触碰 `run/**`。
- 是否执行删除、清理或移动。
- 是否 staging、commit 或 push。
- 验证命令与结果；无法验证时说明原因。
