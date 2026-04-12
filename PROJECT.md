# 项目文档 — claudecode workspace

<!-- PROJECT:SECTION:OVERVIEW -->
## 一、项目总览

本仓库是一个多项目工作区，当前同时承载：

1. Hextech 工作流的 canonical 文档与本地运行态文件。
2. 若干独立项目目录的代码、配置、数据和说明文档。
3. 轻量的本地维护脚本、调试产物与外部归档。

仓库当前的工作流口径是：

- `agents.md` 只保留兼容性摘要，不再是执行端日常主上下文。
- small 任务默认走本地轻量完成。
- large 任务默认走 `Plan Draft → Validation Draft → 分支实现 → PR → Codex 自动审查`。
- `Antigravity` 固定为高难前端执行端、前端专项审查与周期性大型审计。
- 检索任务独立，不再回写 `agents.md` 主台账。

---

<!-- PROJECT:SECTION:FILES -->
## 二、文件职责清单

| 文件 / 目录 | 类型 | 职责 |
| :--- | :--- | :--- |
| `.agents/` | 工作流 canonical 文档 | 存放 Hextech 的技能、合同、适配器、设置与迁移说明 |
| `.claude/CLAUDE.md` | 本地助手入口 | 说明当前仓库的 Hextech 口径与读取路径 |
| `.ai_workflow/` | 运行态目录 | 保存端侧 runtime state、event log、历史归档与辅助脚本 |
| `PROJECT.md` | 根项目总账 | 记录工作区总览、项目职责、风险与变更记录 |
| `run/` | 子项目目录 | Hextech 伴生系统运行目录 |
| `qm-run-demo/run/` | 子项目目录 | 另一套 Hextech 伴生系统/演示目录 |
| `subtitle_extractor/` | 子项目目录 | 本地/在线视频字幕提取工具 |
| `heybox/` | 子项目目录 | 小黑盒网页抓取脚本 |
| `QuantProject/` | 子项目目录 | 多资产量化仓位决策系统 |
| 外部归档目录 | 备份目录 | 本轮工作流部署备份，不作为主流程输入 |

---

<!-- PROJECT:SECTION:DATAFLOW -->
## 三、数据生产、存储与流转

1. Hextech 工作流文档与任务上下文由 `.agents/` 管理。
2. 各项目目录分别维护自己的入口、运行时数据和项目级 `PROJECT.md`。
3. `run/` 与 `qm-run-demo/run/` 以本地数据、缓存、资源和打包产物为主。
4. `subtitle_extractor/` 以音视频输入、字幕转录和 Markdown 输出为主。
5. `heybox/` 将网页抓取结果写入本地 `data/` 目录。
6. `QuantProject/` 将行情 CSV、决策报告和持久化仓位记录保存在本地目录。

---

<!-- PROJECT:SECTION:DEPENDENCIES -->
## 四、关键依赖与影响范围

| 改动文件 / 目录 | 直接影响 | 潜在级联影响 | 审计关注点 |
| :--- | :--- | :--- | :--- |
| `.agents/` | Hextech canonical workflow 行为 | 影响所有执行端口与审查链路 | 是否仍残留旧式主流程口径 |
| `.claude/CLAUDE.md` | Claude Code 入口口径 | 影响 cc 侧任务理解 | 是否与当前轻量工作流一致 |
| `.ai_workflow/` | 运行态、日志与辅助脚本 | 影响恢复、留痕与本地收口 | 是否把旧收尾机制写成默认依赖 |
| 各项目 `PROJECT.md` | 项目文档可读性 | 影响后续 AI 接手与审计效率 | 文件级职责是否足够明确 |
| `run/` / `qm-run-demo/run/` | Hextech UI / Web 运行目录 | 影响打包、展示与资源同步 | 入口薄壳与运行层是否分离 |
| `subtitle_extractor/` | 字幕提取流程 | 影响输出格式与依赖安装 | 本地/在线链路是否清晰 |
| `QuantProject/` | 量化决策与数据同步 | 影响仓位建议与历史归档 | 数据源与回退策略是否清楚 |

---

<!-- PROJECT:SECTION:ISSUES -->
## 五、已知问题、风险与技术债务

| 编号 | 类型 | 来源 | 问题描述 | 影响范围 | 优先级 | 状态 | 建议方案 | 代码锚点 |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| WS-001 | 工作区混合 | 当前仓库结构 | 同一仓库中并存 Hextech 工作流、UI 运行目录、字幕工具、网页抓取脚本和量化脚本，命名风格不完全统一 | 全仓库 | 中 | 已知 | 各项目 `PROJECT.md` 继续维护文件级职责，避免互相串读 | `DEBT[WS-001]` |
| WS-002 | 旧口径残留 | 历史迁移 | 旧版 workflow 说明、脚本和注释里仍可能保留少量历史术语 | `.agents/`、`.ai_workflow/`、少量旧文档 | 中 | 处理中 | 逐步清理活跃引用，历史文件只保留 deprecated 或 archive 标记 | `DEBT[WS-002]` |
| WS-003 | 子项目文档不均衡 | 现有项目状态 | 部分子项目只有少量源码，没有完整 README 或测试说明 | `heybox/`、`QuantProject/` | 低 | 待补 | 以后按项目成熟度补 README / 依赖说明 / 验证步骤 | `DEBT[WS-003]` |

---

<!-- PROJECT:SECTION:CHANGELOG -->
## 六、变更记录

| 日期 | task_id | 执行端 | 最终改动 | 最终有效范围 | 范围变动/新增需求 | 遗留债务 | 审计结果 | 备注 |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| 2026-04-12 | cx-task-workspace-project-doc-refresh-20260412 | cx | 将根项目总账重写为工作区级索引，统一列出 Hextech canonical 文档与各子项目职责 | `PROJECT.md`, `.claude/CLAUDE.md`, `.ai_workflow/` 说明性文件 | 无 | WS-001, WS-002, WS-003 | pending | 本轮仅更新文档与口径，不动业务代码 |

---

<!-- PROJECT:SECTION:MAINTENANCE -->
## 七、维护规则

- 根 `PROJECT.md` 负责工作区级索引，不替代各子项目自己的 `PROJECT.md`。
- 各子项目的 `PROJECT.md` 应优先描述文件级职责、数据流、风险与近期变更。
- Hextech 工作流的 canonical 口径以 `.agents/` 下文件为准。
- 历史退役文件可保留，但不得继续充当主流程默认依赖。
