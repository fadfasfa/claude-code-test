# Agent Skill Inventory

本文件记录当前保留的仓库级 Codex skill。白名单入口是 `.agents/skills/README.md`。

| 名称 | 状态 | 触发场景 |
| :--- | :--- | :--- |
| `karpathy-project-bridge` | keep | 非琐碎代码、脚本、配置或 workflow 实现任务 |
| `karpathy-guardrail` | keep | 非琐碎代码修改、架构/实现/debugging 计划、方案输出防范围漂移 |
| `frontend-design-project-bridge` | keep | 前端 UI / 视觉 / 交互任务 |
| `repo-verification-before-completion` | keep | 声明完成前 |
| `repo-maintenance` | keep | 仓库维护、清理候选、保护资产检查 |
| `repo-local-pr-review` | keep | commit / PR 前本地审查 |
| `repo-module-admission` | keep | 新增 workflow module、skill、hook、tool 或工作区前 |
| `cleanup-worktrees` | keep | 用户显性调用 `$cleanup-worktrees` 或要求审计/清理 PR 合并后的 managed Git worktree / branch 残留 |
| `superpowers-project-bridge` | retired | 旧桥接入口，已迁出 `.agents/skills` 扫描路径 |
| `scrapling-web-scraping` | keep | Scrapling、通用网页抓取、动态网页、结构化抽取、adaptive selector、spider 或现有爬虫替换评估 |

## Retired

- 当前没有需要归档的仓库级 skill。
- 若后续出现未引用、触发不明或重复 skill，先列出影响面并取得用户授权，再删除或退役；不默认新增历史副本。

## Boundary

- `karpathy-guardrail` 负责计划/方案纪律，`karpathy-project-bridge` 负责实现阶段桥接，两者不互相替代。
- 不保留 memory / learning promotion skill。
- 不恢复旧 command、hook、自动 PR shipping、task resume 或高权限 worktree skill；`cleanup-worktrees` 仅作为用户显性调用的 PR 合并残留清理入口，默认只清理已合并、无领先提交、无 tracked 修改、无 `??` untracked，且 ignored 输出为空或仅为白名单 `__pycache__/` 缓存的受管本地对象；`audit` 类请求保持只读。
- `.claude/skills/` 不属于 Codex skill 白名单。
- 官方 Superpowers skills 不在本仓 fork；本仓只认官方已安装 plugin，不再通过本地 bridge 接入方法论。
