# Codex Skills 白名单

本目录是 `claudecode` 仓库级 Codex skill 白名单入口。

当前仓库级 Codex skill：

- `repo-module-admission`：新增模块、skill、hook、tool 或工作区前的准入判断。
- `repo-verification-before-completion`：任务完成前验证证据。
- `repo-local-pr-review`：本地 diff / PR 前审查，不调用云端 PR。
- `repo-maintenance`：仓库维护、清理候选和保护资产检查。
- `karpathy-project-bridge`：桥接全局 `karpathy-guidelines`。
- `superpowers-project-bridge`：极小边界版 Superpowers 桥接。
- `frontend-design-project-bridge`：桥接全局 `frontend-design`。

## 边界

- 不保留 memory / learning promotion。
- 不恢复 command、hook、自动 PR shipping、task resume 或高权限 worktree skill。
- 新增 skill 必须先得到用户明确要求，并走 `repo-module-admission`。
- 任何 skill 都不得覆盖 `AGENTS.md`、`work_area_registry.md`、Git 边界、安全边界或验证要求。
