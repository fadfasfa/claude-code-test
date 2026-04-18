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
- `CLAUDE.md` 负责 Claude Code 的根入口提醒。
- `work_area_registry.md` 负责多工作区登记与默认写入边界。
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
| `CLAUDE.md` | 根入口提醒 | 记录 Claude Code 在本仓的启动与写入边界 |
| `agent_tooling_baseline.md` | 能力基线 | 记录 Claude / Codex 的 skill + CLI 基线与能力边界 |
| `work_area_registry.md` | 工作区注册表 | 记录工作区目的、默认写入边界与新增登记模板 |
| `.claude/` | Claude 项目目录 | 存放项目级 settings、skills 与预留接口说明 |
| `.claude/settings.json` | Claude 本地配置 | 记录当前项目的 Claude Code 配置 |
| `.claude/settings.local.json` | Claude 本地覆写 | 记录项目级本地附加权限 |
| `.claude/skills/` | Claude 项目技能 | 存放当前项目级 skill |
| `.claude/_future/` | 预留接口目录 | 存放未启用的预留接口说明 |
| `.ai_workflow/` | 运行态目录 | 保存端侧 runtime state、event log、历史归档与辅助脚本 |
| `run/` | 子项目目录 | Hextech 伴生系统运行目录 |
| `qm-run-demo/` | 子项目目录 | 另一套 Hextech 伴生系统/演示目录 |
| `subtitle_extractor/` | 子项目目录 | 本地/在线视频字幕提取工具 |
| `heybox/` | 子项目目录 | 小黑盒网页抓取脚本 |
| `QuantProject/` | 子项目目录 | 多资产量化仓位决策系统 |
| 外部归档目录 | 备份目录 | 本轮工作流部署备份，不作为主流程输入 |

---

<!-- PROJECT:SECTION:AGENT_TREE -->
## 三、Agent 相关目录结构

当前仓库内实际存在的 agent 相关结构如下：

```text
.
├── AGENTS.md
├── CLAUDE.md
├── agent_tooling_baseline.md
├── work_area_registry.md
└── .claude/
    ├── settings.json
    ├── settings.local.json
    ├── skills/
    └── _future/
```

- `AGENTS.md`：仓库级稳定规则与 review 入口。
- `CLAUDE.md`：Claude Code 在本仓库的根入口提醒。
- `agent_tooling_baseline.md`：当前阶段的 capability-layer 基线。
- `work_area_registry.md`：当前工作区注册表与新增工作区登记模板。
- `.claude/settings.json`：Claude Code 项目级配置。
- `.claude/settings.local.json`：Claude Code 项目级本地附加权限。
- `.claude/skills/`：当前项目级 skill 目录。
- `.claude/_future/`：未启用预留接口目录。

---

<!-- PROJECT:SECTION:DATAFLOW -->
## 四、数据生产、存储与流转

1. 仓库级稳定规则与 review 入口由根 `AGENTS.md` 管理。
2. Claude Code 的根入口提醒由 `CLAUDE.md` 管理，项目级 settings 与 skills 由 `.claude/` 管理。
3. 项目/工作区结构、职责、数据流与技术债务由根 `PROJECT.md` 管理。
4. 工作区清单与默认写入边界由 `work_area_registry.md` 管理。
5. 各项目目录分别维护自己的入口、运行时数据和项目级 `PROJECT.md`。
6. `run/` 与 `qm-run-demo/` 以本地数据、缓存、资源和打包产物为主。
7. `subtitle_extractor/` 以音视频输入、字幕转录和 Markdown 输出为主。
8. `heybox/` 将网页抓取结果写入本地 `data/` 目录。
9. `QuantProject/` 将行情 CSV、决策报告和持久化仓位记录保存在本地目录。

---

<!-- PROJECT:SECTION:DEPENDENCIES -->
## 五、关键依赖与影响范围

| 改动文件 / 目录 | 直接影响 | 潜在级联影响 | 审计关注点 |
| :--- | :--- | :--- | :--- |
| `PROJECT.md` | 工作区稳定说明 | 影响后续 AI 接手与审计效率 | 是否保持项目级职责单一 |
| `AGENTS.md` | 仓库级稳定规则 | 影响 review 入口与角色边界 | 是否误承载项目结构说明 |
| `CLAUDE.md` | Claude Code 入口口径 | 影响 cc 侧任务理解 | 是否与根入口说明一致 |
| `work_area_registry.md` | 工作区登记与边界 | 影响默认写入范围 | 是否先选定目标工作区 |
| `.claude/settings.json` | Claude 项目配置 | 影响本地 agent 行为 | 是否与当前项目结构匹配 |
| `.claude/skills/` | Claude 项目技能 | 影响项目级能力暴露面 | 是否保持基线范围 |
| `.ai_workflow/` | 运行态、日志与辅助脚本 | 影响恢复、留痕与本地收口 | 是否把旧收尾机制写成默认依赖 |
| `.git/hooks/` | 本地 Git 行为 | 影响提交/合并后的副作用 | 是否仍能触发旧流程 |
| 各项目 `PROJECT.md` | 项目文档可读性 | 影响后续 AI 接手与审计效率 | 文件级职责是否足够明确 |
| `run/` / `qm-run-demo/` | Hextech UI / Web 运行目录 | 影响打包、展示与资源同步 | 入口薄壳与运行层是否分离 |
| `subtitle_extractor/` | 字幕提取流程 | 影响输出格式与依赖安装 | 本地/在线链路是否清晰 |
| `QuantProject/` | 量化决策与数据同步 | 影响仓位建议与历史归档 | 数据源与回退策略是否清楚 |

---

<!-- PROJECT:SECTION:ISSUES -->
## 六、已知问题、风险与技术债务

| 编号 | 类型 | 来源 | 问题描述 | 影响范围 | 优先级 | 状态 | 建议方案 | 代码锚点 |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| WS-001 | 工作区混合 | 当前仓库结构 | 同一仓库中并存 Hextech 稳定规则、UI 运行目录、字幕工具、网页抓取脚本和量化脚本，命名风格不完全统一 | 全仓库 | 中 | 已知 | 各项目 `PROJECT.md` 继续维护文件级职责，避免互相串读 | `DEBT[WS-001]` |
| WS-002 | 历史残留 | 旧流程迁移 | 旧版 workflow 说明、脚本和注释里仍可能保留少量历史术语与旧路径引用 | `.ai_workflow/`、少量旧文档与 git history | 中 | 处理中 | 逐步清理活跃引用；已退役 `.agents/*` 旧入口，历史内容只保留在 git history | `DEBT[WS-002]` |
| WS-003 | 子项目文档不均衡 | 现有项目状态 | 部分子项目只有少量源码，没有完整 README 或测试说明 | `heybox/`、`QuantProject/` | 低 | 待补 | 以后按项目成熟度补 README / 依赖说明 / 验证步骤 | `DEBT[WS-003]` |

---

<!-- PROJECT:SECTION:CHANGELOG -->
## 七、变更记录

| 日期 | task_id | 执行端 | 最终改动 | 最终有效范围 | 范围变动/新增需求 | 遗留债务 | 审计结果 | 备注 |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| 2026-04-13 | cx-task-fix-quantproject-sync-20260413 | cx | 修复 `QuantProject` 数据同步在多线程下调用 yfinance 的不稳定问题，并补 Stooq apikey 检测，恢复 `SPY/QQQ/XAU` 获取链路 | `QuantProject/`, `PROJECT.md` | 无 | WS-003 | pending | 本轮只修同步链路，不调整量化策略口径 |
| 2026-04-13 | cx-task-fix-run-security-audit-20260413 | cx | 修复 `run/` 安全审计中的本地接口认证、Origin/重定向/路径校验、环境变量约束与原子写入问题 | `run/display/`, `run/scraping/`, `run/processing/`, `PROJECT.md` | 无 | WS-001, WS-002, WS-003 | pending | 本轮聚焦高危入口收口与最小验证 |
| 2026-04-13 | cx-task-hextech-lite-finalization-rebuild-20260413 | cx | 将根入口收敛为 `PROJECT.md` + `AGENTS.md`，删除根 `agents.md`，并将规则层与历史层拆分 | `PROJECT.md`, `AGENTS.md`, `.agents/`, `.claude/`, `.git/hooks/` | 删除根兼容壳、收口历史说明 | WS-001, WS-002, WS-003 | pending | 本轮重点是结构收敛与规则分层 |
| 2026-04-13 | cx-task-agents-entry-smoke-2-20260413 | cx | 第二轮 smoke PR：同步根入口关系，确保 `PROJECT.md` 与 `AGENTS.md` 不再指向根 `agents.md` | `PROJECT.md`, `AGENTS.md` | 无 | WS-001, WS-002, WS-003 | pending | 仅用于验证入口同步与自动 review 行为 |
| 2026-04-12 | cx-task-workspace-project-doc-refresh-20260412 | cx | 将根项目总账重写为工作区级索引，统一列出 Hextech canonical 文档与各子项目职责 | `PROJECT.md`, `.claude/CLAUDE.md`, `.ai_workflow/` 说明性文件 | 无 | WS-001, WS-002, WS-003 | pending | 本轮仅更新文档与口径，不动业务代码 |
| 2026-04-15 | cc-task-skill-boundary-governance-20260415 | cc | 收口 Claude 默认 skill 暴露面到 `~/.claude/skills/` + `~/.claude/skills-shared/`，并在 workflow/规则层落地能力字段三元组（主字段 `required_bundles`）与 legacy-compat-bridge 标记 | `.claude/settings.json`, `.agents/skills/workflow_registry.md`, `.agents/skills/decision_playbooks.md`, `.agents/skills/agents_template.md`, `.agents/settings/gemini_setting_core.md`, `AGENTS.md`, `.agents/skills/hextech-workflow.md`, `PROJECT.md` | 无 | WS-002 | pending | 本轮不改用户级 settings、不动代理链、不改 final_review_contract 正文 |
| 2026-04-17 | cc-task-fix-midrun-stop-guard-20260417 | cc | 为 Claude 连续执行链路补齐执行阶段状态字段、真实阻断语义和中途总结 Stop Guard，避免执行型任务在阶段总结后提前停下 | `.claude/CLAUDE.md`, `.claude/hooks/runtime_state_writer.sh`, `.claude/hooks/stop_guard.sh`, `PROJECT.md` | 无 | WS-002 | pending | 本轮只修执行态与 stop 判定，不改 visual_frozen 主规则 |
| 2026-04-17 | cc-task-run-manifest-export-20260417 | cc | 为 `run/` 抓取链路新增技术兵格式派生产物：职业图片清单、按职业分组天赋图标与天赋图标索引，并保持现有 Web/API 主链路不变 | `run/scraping/version_sync.py`, `run/scraping/augment_catalog.py`, `run/data/manifests/`, `PROJECT.md` | 新增派生产物目录与中文 manifest 文件 | WS-001 | pending | 职业图片清单来自英雄头像资源；天赋详细描述走 tooltip_plain→tooltip→description 回退链；天赋分组现优先依据 `Champion_Hextech_Cache.json` 反向推导真实英雄归属，索引中保留 `hero_names` 列表，缓存缺项时回退到 tier |

---

<!-- PROJECT:SECTION:MAINTENANCE -->
## 八、维护规则

- 根 `PROJECT.md` 负责工作区级稳定说明，不替代各子项目自己的 `PROJECT.md`。
- 根 `AGENTS.md` 只负责仓库级稳定规则、角色边界与 review 入口，不承载项目结构说明。
- 根 `CLAUDE.md` 只负责 Claude Code 的根入口提醒，不替代工作区文档。
- `work_area_registry.md` 负责登记工作区、默认写入边界与新增工作区模板。
- 各子项目的 `PROJECT.md` 应优先描述文件级职责、数据流、风险与近期变更。
- `.claude/` 只承载 Claude Code 的项目级配置、skills 与预留接口，不替代根入口文档。
- agent 相关目录结构应以仓库内实际存在的 `AGENTS.md`、`CLAUDE.md`、`agent_tooling_baseline.md`、`work_area_registry.md` 与 `.claude/` 为准，避免继续把已退役的 `.agents/*` 旧路径当作活跃入口。
