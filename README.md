# claudecode

`claudecode` 是一个多功能本地开发仓库。它包含多个独立工作区，也包含 repo-local Claude Code 工作流规则。

## 入口

- `AGENTS.md`：agent 规则、边界、git 确认规则
- `CLAUDE.md`：Claude Code 入口和读取顺序
- `PROJECT.md`：仓库地图
- `work_area_registry.md`：业务工作区注册表
- `agent_tooling_baseline.md`：repo-local tooling baseline
- `docs/task-routing.md`：小 / 中 / 大任务路由
- `docs/safety-boundaries.md`：安全边界

## 定期维护

本仓只保留两个项目级维护入口：`/maintenance` 和 `/promote-learning`。

`/maintenance` 用于定期盘点 `.tmp` 临时目录，并通过 `/maintenance learning` 从 `.learnings/ERRORS.md` 提炼候选经验。该命令默认只读，不删除、不写入、不提交。详见 `docs/maintenance.md`。

稳定 learning 需要审查是否晋升到 docs、skills 或入口规则时，使用 skill-style slash command `/promote-learning`；默认只输出 patch plan。

## 运行模型

- 小任务和明确 bug 修复走轻量 patch-and-verify 流程。
- 中任务走简短计划和明确验证。
- 大任务才进入需求收敛、轻量 brainstorm / 方案比较、任务拆分、worktree 隔离、TDD、subagent 并行和 PR-style review。

本仓不继承 `kb` 工作流，也不修改全局 Claude Code / Codex / Superpowers / ECC 层。

## 业务修改前

1. 检查 `git status --short --branch`。
2. 读取 `work_area_registry.md`。
3. 声明 `target_work_area`。
4. 使用最近的 repo-native 验证。

不要把重流程用于琐碎修改。
