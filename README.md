# claudecode

`claudecode` 是多工作区本地开发仓库。Codex 是当前唯一主流程。

Claude Code 只保留空白占位。

## 入口

- `AGENTS.md`：当前 Codex 规则。
- `PROJECT.md`：仓库地图。
- `work_area_registry.md`：工作区注册表和写入边界。
- `agent_tooling_baseline.md`：工具基线。
- `docs/task-routing.md`：任务分级和路由。
- `docs/safety-boundaries.md`：安全边界。
- `docs/module-admission.md`：新增仓库级能力准入。
- `.agents/skills/README.md`：仓库级 Codex skill 白名单。

## 业务修改前

1. 查看 `git status --short`。
2. 从 `work_area_registry.md` 选择 `target_work_area`。
3. 写入限制在选定范围内；治理文档任务除外。
4. 完成前运行最接近风险面的验证。

小改动不需要重流程。
