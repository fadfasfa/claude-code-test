# Agent Skill Inventory

本文件说明当前保留的仓库级 Codex skill。它是 inventory，不替代 `.agents/skills/README.md` 白名单，也不覆盖 `AGENTS.md`。

| 名称 | 作用 | 触发场景 | 使用者 | 默认启用 |
| :--- | :--- | :--- | :--- | :--- |
| `karpathy-project-bridge` | 桥接全局 `karpathy-guidelines` | 非琐碎代码、脚本、配置或 workflow 实现任务 | Codex | 是，代码任务强制 |
| `frontend-design-project-bridge` | 桥接全局 `frontend-design` | 前端 UI / 视觉 / 交互任务 | Codex | 仅前端任务 |
| `repo-verification-before-completion` | 完成前汇总验证证据和剩余风险 | 声明完成前 | Codex | 是，非琐碎任务收尾 |
| `repo-maintenance` | 仓库维护、清理候选、保护资产检查 | 维护、治理、清理候选和健康检查 | Codex | 按任务触发 |
| `repo-local-pr-review` | 本地 diff / PR 前审查，不调用云端 PR | commit / PR 前本地审查 | Codex | 按任务触发 |
| `repo-module-admission` | 新增长期能力前的准入判断 | 新增 workflow module、skill、hook、tool 或工作区前 | Codex | 按任务触发 |
| `superpowers-project-bridge` | 方法论 router 桥接 | 明确提到 Superpowers 或需路由 brainstorming / worktree / planning 等方法 | Codex | 按任务触发 |

## 边界

- 不保留 memory / learning promotion skill。
- 不恢复 command、hook、自动 PR shipping、task resume 或高权限 worktree skill。
- Worktree 策略以 `docs/workflows/worktree-policy.md` 为唯一当前策略。
- `.claude/skills/` 只服务 Claude Code，不属于 Codex skill 白名单。
