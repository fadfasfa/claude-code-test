# Codex Skills 白名单

本目录是 `claudecode` 仓库级 Codex skill 白名单入口。

当前保留的仓库级 skill：

- `karpathy-project-bridge`
- `frontend-design-project-bridge`
- `repo-verification-before-completion`
- `repo-maintenance`
- `repo-local-pr-review`
- `repo-module-admission`
- `crawl4ai-web-scraping`

详细触发场景见 `docs/workflows/agent-skill-inventory.md`。没有列入本 README 的 skill 不视为默认启用。

## Boundary

- 不保留 memory / learning promotion。
- 不恢复 command、hook、自动 PR shipping、task resume 或高权限 worktree skill。
- 新增 skill 必须先得到用户明确要求，并走 `repo-module-admission`。
- 不保留 Superpowers bridge skill。
- 其他 skill 不得覆盖 `AGENTS.md`、`docs/workflows/work_area_registry.md`、Git 边界、安全边界、发布权限、验收规则或 workflow scripts。

