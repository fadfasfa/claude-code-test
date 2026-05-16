# claudecode

`claudecode` 是多工作区本地开发仓库。Codex 是当前唯一主流程。

Claude Code 只保留空白占位。

## Repository map

| 路径 | 作用 | 面向对象 | 手工修改 | 可删除 | 典型入口命令 |
| :--- | :--- | :--- | :--- | :--- | :--- |
| `README.md` | 人类快速入口和目录说明 | 人 | 可以，小步改 | 否 | `Get-Content README.md` |
| `PROJECT.md` | agent 仓库地图和工作区概览 | agent | 可以，小步改 | 否 | `Get-Content PROJECT.md` |
| `AGENTS.md` | Codex 当前规则和边界 | agent / Codex | 谨慎改 | 否 | `Get-Content AGENTS.md` |
| `CLAUDE.md` | Claude Code 项目入口和 CC 边界 | CC | 谨慎改 | 否 | `Get-Content CLAUDE.md` |
| `cx-exec.ps1` | CC -> CX 根入口 delegator | CC / 脚本 | 谨慎改 | 否 | `.\cx-exec.ps1 -TaskId demo -TaskDescription "..." -DryRun` |
| `docs/` | 短索引、工作流、reference 和 archive | 人 / agent | 可以 | 否 | `Get-Content docs\index.md` |
| `docs/workflows/` | 工作流规则、注册表、工具基线、协作说明 | 人 / agent | 可以 | 否 | `Get-ChildItem docs\workflows` |
| `docs/reference/` | 长文规则、learning 摘要和可选参考 | 人 / agent | 谨慎读 | 否 | `Get-ChildItem docs\reference` |
| `docs/archive/` | 历史报告、旧方案和退休日志 | 人 / agent | 默认不读 | 否 | `Get-ChildItem docs\archive` |
| `scripts/` | 仓库级辅助脚本 | 脚本 / agent | 谨慎改 | 否 | `Get-ChildItem scripts` |
| `scripts/workflow/` | worktree、verify、local review、CC -> CX 执行器 | 脚本 / CC | 谨慎改 | 否 | `.\scripts\workflow\verify.ps1` |
| `run/` | Hextech 业务运行区 | 脚本 / agent | 按注册表改 | 否 | `Get-ChildItem run` |
| `.state/workflow/` | CC -> CX 本地运行态和本轮报告 | 运行态 / CC | 不手改任务结果 | 运行态子目录可清理 | `Get-ChildItem .state\workflow` |
| `.agents/` | 仓库级 Codex skill 白名单和桥接 skill | agent | 谨慎改 | 否 | `Get-Content .agents\skills\README.md` |
| `.claude/` | Claude Code 空白占位和必要本地接口 | CC | 通常不改 | 否 | `Get-ChildItem .claude` |
| `.codex/` | Codex 项目配置占位；不放运行态 | Codex | 通常不改 | 否 | `Get-ChildItem .codex` |
| `.githooks/` | 本仓 Git hooks | Git | 谨慎改 | 否 | `Get-ChildItem .githooks` |
| `heybox/` | 独立仓库目录，本地抓取脚本 | 子项目 | 只在该项目任务中改 | 否 | `git -C heybox status --short` |
| `qm-run-demo/` | 独立仓库目录，demo / runtime 变体 | 子项目 | 只在该项目任务中改 | 否 | `git -C qm-run-demo status --short` |
| `QuantProject/` | 独立本地私有工作区 | 子项目 | 只在明确授权时改 | 否 | `git -C QuantProject status --short` |
| `sm2-randomizer/` | 独立业务项目 | 子项目 | 只在该项目任务中改 | 否 | `git -C sm2-randomizer status --short` |
| `subtitle_extractor/` | 独立工具项目 | 子项目 | 只在该项目任务中改 | 否 | `git -C subtitle_extractor status --short` |

## Workflow entrypoints

- Codex 独立工作：直接按 `AGENTS.md` 和任务上下文执行。
- Claude Code 调用 Codex：从仓库根目录运行 `.\cx-exec.ps1`，结构化结果写入 `.state/workflow/tasks/<task_id>/`。
- 默认先读 `docs/index.md`；不要自动读取 `docs/archive/` 或 `docs/reference/` 下的长文，除非任务明确需要。

## 业务修改前

1. 查看 `git status --short`。
2. 从 `docs/workflows/work_area_registry.md` 选择 `target_work_area`。
3. 写入限制在选定范围内；治理文档任务除外。
4. 完成前运行最接近风险面的验证。

小改动不需要重流程。

