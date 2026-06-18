# Codex Skills 白名单

本目录是 `claudecode` 仓库级 Codex skill 白名单入口。

当前保留的仓库级 skill：

- `karpathy-project-bridge`
- `frontend-design-project-bridge`
- `repo-verification-before-completion`
- `repo-maintenance`
- `repo-local-pr-review`
- `repo-module-admission`
- `cleanup-worktrees`
- `scrapling-web-scraping`

详细触发场景见 `docs/workflows/agent-skill-inventory.md`。没有列入本 README 的 skill 不视为默认启用。

## Boundary

- 不保留 memory / learning promotion。
- 不恢复旧 command、hook、自动 PR shipping、task resume 或高权限 worktree skill；`cleanup-worktrees` 只作为用户显性调用的 PR 合并残留清理入口，默认只清理已合并、无领先提交、无 tracked 修改、无 `??` untracked，且 ignored 输出为空或仅为白名单 `__pycache__/` 缓存的受管本地对象；`audit` 类请求保持只读。
- 新增 skill 必须先得到用户明确要求，并走 `repo-module-admission`。
- 不保留 Superpowers bridge skill。
- 其他 skill 不得覆盖 `AGENTS.md`、`docs/workflows/work_area_registry.md`、Git 边界、安全边界、发布权限、验收规则或 workflow scripts。

