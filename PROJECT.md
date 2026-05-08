# claudecode 项目地图

`claudecode` 是多工作区本地开发仓库。仓库根目录是治理和路由层，不是默认业务实现区。

Codex 是当前唯一主流程。Claude Code 只保留空白占位。

## 根目录职责

- `AGENTS.md`：当前 Codex 规则和边界。
- `CLAUDE.md`：空白占位。
- `PROJECT.md`：仓库地图。
- `README.md`：快速入口。
- `work_area_registry.md`：工作区注册表和写入边界。
- `agent_tooling_baseline.md`：当前工具基线。
- `docs/`：当前工作流、安全、验证和准入文档。
- `.agents/skills/`：仓库级 Codex skill 白名单。

## 工作区

| 路径 | 用途 |
| :--- | :--- |
| `run/` | Hextech 运行时、抓取、处理、展示和工具 |
| `sm2-randomizer/` | Space Marine 2 随机器应用和数据管线 |
| `QuantProject/` | 量化策略和数据工作区 |
| `heybox/` | 本地抓取脚本 |
| `qm-run-demo/` | demo / runtime 变体工作区 |
| `subtitle_extractor/` | 字幕提取工作区 |
| `.learnings/` | 仓库内备注和已忽略原始错误输入 |
| `.tmp/` | 已忽略运行时临时空间 |
| `docs/` | 仓库工作流、安全、路由和验证规则 |

业务写入前必须先从 `work_area_registry.md` 选择目标工作区。

## 当前文档

| 文件 | 用途 |
| :--- | :--- |
| `docs/task-routing.md` | 任务分级和路由 |
| `docs/safety-boundaries.md` | 仓库、Git、全局、KB 和凭据边界 |
| `docs/module-admission.md` | 新增仓库级能力的准入模板 |
| `docs/continuous-execution.md` | 已接受任务的继续执行和阻断规则 |
| `docs/frontend-validation.md` | 前端验证规则 |
| `docs/playwright-policy.md` | Playwright 使用边界 |

## 非目标

- 不用普通仓库任务修改全局工具配置。
- 不把本仓工作流推入 KB 仓库。
- 不恢复 command、hook、memory、learning、PR shipping、task resume 或 worktree 高权限能力。
- 不在仓库根目录创建小写 `agents.md` 或临时任务笔记。
