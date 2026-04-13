# 项目文档 — claudecode workspace

<!-- PROJECT:SECTION:OVERVIEW -->
## 一、项目总览

本仓库是一个多项目工作区，当前同时承载：

1. Hextech 工作流的稳定规则、审查合同与本地运行态文件。
2. 若干独立项目目录的代码、配置、数据和说明文档。
3. 轻量的本地维护脚本、调试产物与历史归档。

本文件只记录项目/工作区级稳定说明，不承载任务态、会话态或执行态。

已采用的稳定约束：

- `PROJECT.md` 负责项目结构、文件职责、数据流、技术债务、近期变更原则。
- `AGENTS.md` 负责仓库级稳定规则与 review 入口。
- `small` 任务默认走本地轻量完成。
- `large` 任务默认走 `Plan Draft → Validation Draft → 分支实现 → PR → Codex 自动审查`。
- `retrieval` 任务独立，不进入代码任务链路。
- `Antigravity` 固定为高难前端执行端、前端专项审查与周期性大型审计。
- Claude / Claude Code 主要承担文件生成与辅助验证，不作为当前主执行端。

---

<!-- PROJECT:SECTION:FILES -->
## 二、文件职责清单

| 文件 / 目录 | 类型 | 职责 |
| :--- | :--- | :--- |
| `PROJECT.md` | 项目/工作区说明 | 记录结构、职责、数据流、风险、技术债务与近期变更原则 |
| `AGENTS.md` | 仓库级规则 | 记录稳定规则、角色边界、review 入口 |
| `.agents/contracts/` | 现行合同 | 记录最终审查合同与 Antigravity 契约 |
| `.agents/adapters/` | 现行适配层 | 记录 Codex 专属审查适配层 |
| `.agents/skills/` | 现行工作流与模板 | 记录 registry、工作流协议、临时模板 |
| `.agents/archive/` | 历史归档 | 存放已退役的旧流程说明、旧术语与迁移记录 |
| `.claude/CLAUDE.md` | 本地助手入口 | 说明当前仓库的 Hextech 口径与读取路径 |
| `.ai_workflow/` | 运行态目录 | 保存端侧 runtime state、event log、历史归档与辅助脚本 |
| `run/` | 子项目目录 | Hextech 伴生系统运行目录 |
| `qm-run-demo/run/` | 子项目目录 | 另一套 Hextech 伴生系统/演示目录 |
| `subtitle_extractor/` | 子项目目录 | 本地/在线视频字幕提取工具 |
| `heybox/` | 子项目目录 | 小黑盒网页抓取脚本 |
| `QuantProject/` | 子项目目录 | 多资产量化仓位决策系统 |
| 外部归档目录 | 备份目录 | 本轮工作流部署备份，不作为主流程输入 |

---

<!-- PROJECT:SECTION:DATAFLOW -->
## 三、数据生产、存储与流转

1. 稳定规则与审查合同由 `AGENTS.md` 和 `.agents/contracts/` 管理。
2. 项目/工作区结构、职责、数据流与技术债务由根 `PROJECT.md` 管理。
3. 各项目目录分别维护自己的入口、运行时数据和项目级 `PROJECT.md`。
4. `run/` 与 `qm-run-demo/run/` 以本地数据、缓存、资源和打包产物为主。
5. `subtitle_extractor/` 以音视频输入、字幕转录和 Markdown 输出为主。
6. `heybox/` 将网页抓取结果写入本地 `data/` 目录。
7. `QuantProject/` 将行情 CSV、决策报告和持久化仓位记录保存在本地目录。
8. 历史迁移说明与退役流程说明统一进入 `.agents/archive/`。

---

<!-- PROJECT:SECTION:DEPENDENCIES -->
## 四、关键依赖与影响范围

| 改动文件 / 目录 | 直接影响 | 潜在级联影响 | 审计关注点 |
| :--- | :--- | :--- | :--- |
| `PROJECT.md` | 工作区稳定说明 | 影响后续 AI 接手与审计效率 | 是否保持项目级职责单一 |
| `AGENTS.md` | 仓库级稳定规则 | 影响 review 入口与角色边界 | 是否误承载项目结构说明 |
| `.agents/contracts/` | 审查合同 | 影响 Codex / Antigravity 审查结果 | 是否仍残留旧台账或锁态依赖 |
| `.agents/adapters/` | 审查适配层 | 影响 PR 审核执行 | 是否与合同一致 |
| `.agents/skills/` | 工作流协议与模板 | 影响任务摘要、路由与临时产物 | 是否把旧流程写回默认路径 |
| `.claude/CLAUDE.md` | Claude Code 入口口径 | 影响 cc 侧任务理解 | 是否与新双入口一致 |
| `.ai_workflow/` | 运行态、日志与辅助脚本 | 影响恢复、留痕与本地收口 | 是否把旧收尾机制写成默认依赖 |
| `.git/hooks/` | 本地 Git 行为 | 影响提交/合并后的副作用 | 是否仍能触发旧流程 |
| 各项目 `PROJECT.md` | 项目文档可读性 | 影响后续 AI 接手与审计效率 | 文件级职责是否足够明确 |
| `run/` / `qm-run-demo/run/` | Hextech UI / Web 运行目录 | 影响打包、展示与资源同步 | 入口薄壳与运行层是否分离 |
| `subtitle_extractor/` | 字幕提取流程 | 影响输出格式与依赖安装 | 本地/在线链路是否清晰 |
| `QuantProject/` | 量化决策与数据同步 | 影响仓位建议与历史归档 | 数据源与回退策略是否清楚 |

---

<!-- PROJECT:SECTION:ISSUES -->
## 五、已知问题、风险与技术债务

| 编号 | 类型 | 来源 | 问题描述 | 影响范围 | 优先级 | 状态 | 建议方案 | 代码锚点 |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| WS-001 | 工作区混合 | 当前仓库结构 | 同一仓库中并存 Hextech 稳定规则、UI 运行目录、字幕工具、网页抓取脚本和量化脚本，命名风格不完全统一 | 全仓库 | 中 | 已知 | 各项目 `PROJECT.md` 继续维护文件级职责，避免互相串读 | `DEBT[WS-001]` |
| WS-002 | 历史残留 | 旧流程迁移 | 旧版 workflow 说明、脚本和注释里仍可能保留少量历史术语 | `.agents/archive/`、`.ai_workflow/`、少量旧文档 | 中 | 处理中 | 逐步清理活跃引用，历史文件只保留 deprecated 或 archive 标记 | `DEBT[WS-002]` |
| WS-003 | 子项目文档不均衡 | 现有项目状态 | 部分子项目只有少量源码，没有完整 README 或测试说明 | `heybox/`、`QuantProject/` | 低 | 待补 | 以后按项目成熟度补 README / 依赖说明 / 验证步骤 | `DEBT[WS-003]` |

---

<!-- PROJECT:SECTION:CHANGELOG -->
## 六、变更记录

| 日期 | task_id | 执行端 | 最终改动 | 最终有效范围 | 范围变动/新增需求 | 遗留债务 | 审计结果 | 备注 |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| 2026-04-13 | cx-task-fix-run-security-audit-20260413 | cx | 修复 `run/` 安全审计中的本地接口认证、Origin/重定向/路径校验、环境变量约束与原子写入问题 | `run/display/`, `run/scraping/`, `run/processing/`, `PROJECT.md` | 无 | WS-001, WS-002, WS-003 | pending | 本轮聚焦高危入口收口与最小验证 |
| 2026-04-13 | cx-task-hextech-lite-finalization-rebuild-20260413 | cx | 将根入口收敛为 `PROJECT.md` + `AGENTS.md`，删除根 `agents.md`，并将规则层与历史层拆分 | `PROJECT.md`, `AGENTS.md`, `.agents/`, `.claude/`, `.git/hooks/` | 删除根兼容壳、收口历史说明 | WS-001, WS-002, WS-003 | pending | 本轮重点是结构收敛与规则分层 |
| 2026-04-13 | cx-task-agents-entry-smoke-2-20260413 | cx | 第二轮 smoke PR：同步根入口关系，确保 `PROJECT.md` 与 `AGENTS.md` 不再指向根 `agents.md` | `PROJECT.md`, `AGENTS.md` | 无 | WS-001, WS-002, WS-003 | pending | 仅用于验证入口同步与自动 review 行为 |
| 2026-04-12 | cx-task-workspace-project-doc-refresh-20260412 | cx | 将根项目总账重写为工作区级索引，统一列出 Hextech canonical 文档与各子项目职责 | `PROJECT.md`, `.claude/CLAUDE.md`, `.ai_workflow/` 说明性文件 | 无 | WS-001, WS-002, WS-003 | pending | 本轮仅更新文档与口径，不动业务代码 |

---

<!-- PROJECT:SECTION:MAINTENANCE -->
## 七、维护规则

- 根 `PROJECT.md` 负责工作区级稳定说明，不替代各子项目自己的 `PROJECT.md`。
- 根 `AGENTS.md` 只负责仓库级稳定规则、角色边界与 review 入口，不承载项目结构说明。
- 各子项目的 `PROJECT.md` 应优先描述文件级职责、数据流、风险与近期变更。
- Hextech 稳定规则以 `AGENTS.md` 与 `.agents/contracts/` 为准，工作流模板与路由以 `.agents/skills/` 为准。
- 历史退役文件可保留在 `.agents/archive/`，但不得继续充当主流程默认依赖。
