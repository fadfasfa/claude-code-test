# claudecode 项目地图

`claudecode` 是个人总编程仓、多子项目母仓和 Codex 编程主执行仓。仓库根目录承载规则、路由、工作流文档和工具脚本，不承载默认业务实现。

Codex 是当前唯一主流程。Claude Code 只保留白板和必要接口。

## 根目录职责

- `AGENTS.md`：当前 Codex 规则和边界。
- `CLAUDE.md`：空白占位。
- `PROJECT.md`：仓库地图。
- `README.md`：快速入口。
- `docs/workflows/work_area_registry.md`：工作区注册表和写入边界。
- `docs/workflows/agent_tooling_baseline.md`：当前工具基线。
- `docs/`：短索引、当前工作流、reference 和 archive。
- `.state/workflow/`：CC 调用 Codex 时的任务结果、状态、报告和归档目录；根目录和 `run/` 不再作为临时产物区。
- `docs/workflows/`：个人总编程仓工作流骨架。
- `scripts/git/`：Git / worktree 辅助脚本。
- `scripts/workflow/`：本仓工作流入口脚本。
- `cx-exec.ps1`：CC -> Codex 根目录 delegator，转发到 `scripts/workflow/cx-exec.ps1`。
- `.agents/skills/`：仓库级 Codex skill 白名单。

## 工作区

| 路径 | 定位 | 说明 |
| :--- | :--- | :--- |
| `run/` | 业务项目 / 数据区域 | Hextech 运行时、抓取、处理、展示和工具；不承载仓库级 workflow 运行态 |
| `sm2-randomizer/` | 业务项目 | Space Marine 2 随机器应用和数据管线 |
| `QuantProject/` | 业务项目 / 数据区域 | 量化策略和数据工作区；业务规则不写入仓库根规则 |
| `heybox/` | 业务项目 / 爬虫工具 | 本地抓取脚本 |
| `qm-run-demo/` | 实验区 / demo | demo / runtime 变体工作区 |
| `subtitle_extractor/` | 工具区 | 字幕提取工作区 |
| `.state/workflow/` | 运行态 | CC -> CX 任务结果、状态、日志和本轮 reports |
| `docs/reference/learnings/` | 备注区 | 仓库内 learning 摘要；不再放根目录 `.learnings/` |
| `docs/archive/learnings-retired/` | 历史归档 | 退休的原始错误输入，默认不注入上下文 |
| `docs/` | 治理区 | 短索引、工作流、安全、路由、验证规则和历史归档 |
| `scripts/` | 工具区 | 仓库级辅助脚本 |

业务写入前必须先从 `docs/workflows/work_area_registry.md` 选择目标工作区。QuantProject 等子项目保留自己的业务规则，根目录只写跨项目流程和边界。

## 当前文档

| 文件 | 用途 |
| :--- | :--- |
| `docs/index.md` | docs 短索引和上下文控制入口 |
| `docs/reference/policies/task-routing.md` | 任务分级和路由 |
| `docs/reference/policies/safety-boundaries.md` | 仓库、Git、全局、KB 和凭据边界 |
| `docs/reference/policies/module-admission.md` | 新增仓库级能力的准入模板 |
| `docs/reference/policies/continuous-execution.md` | 已接受任务的继续执行和阻断规则 |
| `docs/reference/policies/frontend-validation.md` | 前端验证规则 |
| `docs/reference/policies/playwright-policy.md` | Playwright 使用边界 |
| `docs/workflows/00-overview.md` | Full Workflow Bootstrap 总览 |
| `docs/workflows/cc-cx-collaboration.md` | Claude Code 与 Codex 协作链路 |
| `docs/workflows/repository-layout.md` | 根目录治理和目录职责说明 |

## 非目标

- 不用普通仓库任务修改全局工具配置。
- 不把本仓工作流推入 KB 仓库。
- 不把子项目业务规则写入仓库根规则。
- 不恢复 command、hook、memory、learning、自动 PR shipping、task resume 或高权限 worktree 能力。
- 不在仓库根目录创建小写 `agents.md` 或临时任务笔记。

