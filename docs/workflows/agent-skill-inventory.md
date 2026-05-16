# Agent Skill Inventory

本文件记录当前保留的仓库级 Codex skill。白名单入口仍是 `.agents/skills/README.md`。

| 名称 | 状态 | 触发场景 | 说明 |
| :--- | :--- | :--- | :--- |
| `karpathy-project-bridge` | keep | 非琐碎代码、脚本、配置或 workflow 实现任务 | 桥接全局 `karpathy-guidelines` |
| `frontend-design-project-bridge` | keep | 前端 UI / 视觉 / 交互任务 | 桥接全局 `frontend-design` |
| `repo-verification-before-completion` | keep | 声明完成前 | 汇总验证证据、禁止路径和剩余风险 |
| `repo-maintenance` | keep | 仓库维护、清理候选、保护资产检查 | 默认只读或 dry-run |
| `repo-local-pr-review` | keep | commit / PR 前本地审查 | 不调用云端 PR |
| `repo-module-admission` | keep | 新增 workflow module、skill、hook、tool 或工作区前 | 准入判断 |
| `superpowers-project-bridge` | keep | 明确提到 Superpowers 或需方法路由 | 只路由方法；worktree 规则链接 canonical policy |

## Retired / Candidate

当前 `.agents/skills/` 下没有需要归档的未引用 skill。若后续发现未引用、触发不明或重复的 skill，归档到 `docs/archive/skills-retired/`，并在本表标记 `retired` 或 `candidate`。

## Boundary

- 不保留 memory / learning promotion skill。
- 不恢复 command、hook、自动 PR shipping、task resume 或高权限 worktree skill。
- Worktree 策略以 `docs/workflows/worktree-policy.md` 为当前策略。
- `.claude/skills/` 只服务 Claude Code，不属于 Codex skill 白名单。
