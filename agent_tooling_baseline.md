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

- Codex 走全局配置。
- 全局配置已使用 `codex-proxy`、`CODEX_PROXY_API_KEY` 和 Responses API。
- 全局根层 `model_provider = "codex-proxy"` 是 VS Code Codex 插件和 Codex CLI 的默认 provider 来源；profiles 只是工作模式切换，不承担默认 provider 修复职责。
- Codex App 使用 ChatGPT 账号登录，负责插件管理、App UI、cloud/thread/desktop 类能力；API key / `codex-proxy` 登录下如果插件或 cloud 能力不可用，不再尝试强行绕过。
- VS Code Codex 插件和 Codex CLI 是本地执行主力，固定通过全局 `codex-proxy` provider 使用 `CODEX_PROXY_API_KEY`，不跟随 Codex App 当前 ChatGPT 登录账号切换。
- `cockpit-tools` 只允许切换 Codex App / ChatGPT 登录态，不修改 VS Code Codex 插件配置、全局 `codex-proxy` provider、`CODEX_PROXY_API_KEY` 或 VS 插件认证状态。
- 日常代码额度通过 `codex-proxy` 账号池消耗；Codex App 的 ChatGPT 账号额度只用于插件、云端、桌面和 App 专属能力。
- 默认权限口径是低摩擦 `granular`：普通工作区读写、脚本、lint、test、build 和已启用网络不频繁询问。
- `rules/default.rules` 是高危动作闸门；发布、破坏性 Git、删除、敏感边界必须 prompt 或 forbidden。
- `full-access` profile 只能人工选择，不是任何仓库的默认权限口径。
- 本仓不再设置 repo-local `.codex/config.toml`，也不得恢复该文件作为默认项目配置。
- `frontend-design` 全局 skill 已可见；本仓只保留 `frontend-design-project-bridge` 作为触发边界说明。
- 全局 `karpathy-guidelines` 可由 `karpathy-project-bridge` 桥接。
- Superpowers 可用，但只作为能力索引，不覆盖本仓规则。

## 边界

- 普通仓库任务不得修改全局 Claude Code、Codex、Superpowers、ECC、CLI、VS plugin、Codex App 或代理配置。
- 不用 `cockpit-tools` 修改 VS Code Codex 插件配置、全局 `codex-proxy` provider、`CODEX_PROXY_API_KEY`、VS 插件认证状态或 CLI 的 `codex-proxy` 默认执行链路。
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
