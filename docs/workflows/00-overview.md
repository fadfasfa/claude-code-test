# Full Workflow Bootstrap v1

本目录记录 `claudecode` 作为个人总编程仓的可执行工作流骨架。

## 目标

- 让任务从入口、工作区选择、验证、审查到发布准备都有固定路径。
- 保护业务数据、密钥和不可重建资产。
- 保持根目录为治理层，不承载子项目业务规则。

## 默认流程

1. 读 `AGENTS.md`、`PROJECT.md`、`work_area_registry.md`。
2. 选择明确 `target_work_area`。
3. 使用 `scripts/workflow/worktree-start.ps1` 创建单一 detached active worktree。
4. 小步修改，避免顺手重构。
5. 运行 `scripts/workflow/verify.ps1` 或说明无法验证的原因。
6. 使用 `scripts/workflow/local-review.ps1` 做本地审查摘要并写入 acceptance gate。
7. 需要时通过 `TASK_HANDOFF.md` 快速人工验收；`automated` 不强制打开 VS Code。
8. 发布类动作必须由用户明确授权。
## 认证与额度分离

- Codex App 使用 ChatGPT 账号登录，负责插件管理、Codex App UI、cloud/thread/desktop 类能力。API key / `codex-proxy` 登录下如果插件或 cloud 能力不可用，不再尝试强行绕过。
- VS Code Codex 插件和 Codex CLI 作为本地执行主力，固定通过全局 `codex-proxy` provider 使用 `CODEX_PROXY_API_KEY`。它们不跟随 Codex App 当前 ChatGPT 登录账号切换。
- `cockpit-tools` 只允许切换 Codex App / ChatGPT 登录态，不修改 VS Code Codex 插件配置、不修改全局 `codex-proxy` provider、不修改 `CODEX_PROXY_API_KEY`、不触碰 VS 插件认证状态。
- 日常代码额度通过 `codex-proxy` 账号池消耗；Codex App 的 ChatGPT 账号额度只用于插件、云端、桌面和 App 专属能力。

## 停止条件

- 目标路径不清。
- 涉及凭据、token、cookie、proxy secret 或私有配置。
- 备份失败。
- 验证命令无法运行且没有明确原因。
- 将要触碰未授权业务工作区或当前脏树。
- 任务目标路径与主仓 dirty 文件重叠，且用户未显式选择处理方式。

## Canonical Entrypoints

- `scripts/workflow/*.ps1` 是当前 canonical workflow。
- `scripts/git/ccw-*.ps1` 是 legacy compatibility，只保留兼容，不作为默认入口。
- 后续是否包装、弃用或迁移 legacy scripts，需要单独任务决定。
