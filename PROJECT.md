# 项目文档 — claude-code-test

<!-- PROJECT:SECTION:OVERVIEW -->
## 一、项目总览

本仓库当前作为多项目混合工作区与 Hextech 工作流落地仓库使用。

目标分为两部分：

1. 作为实际代码与脚本工作区，承载多个子目录与项目内容。
2. 作为 Hextech V5.3 工作流的运行容器，统一管理任务卡、运行状态、事件日志、合并后归档与待机重置。

当前优先事项不是新增业务功能，而是先把工作流主线落地，确保以下链路成立：

`agents.md` 待机壳 → 任务激活 → 修改执行 → 更新 `PROJECT.md` → 提交 / PR → 合并后归档 → 回到 standby

---

<!-- PROJECT:SECTION:FILES -->
## 二、文件职责清单

| 文件 | 类型 | 职责 |
| :--- | :--- | :--- |
| `agents.md` | 工作流任务总表 | 当前任务卡 / 待机壳；记录任务状态、执行模式、评审路径与任务台账 |
| `PROJECT.md` | 项目总账 | 记录项目总览、关键文件职责、依赖影响、风险与变更记录 |
| `.ai_workflow/runtime_state_cx.json` | 运行状态文件 | 记录 Codex 端当前运行状态 |
| `.ai_workflow/event_log_cx.jsonl` | 事件日志 | 记录 Codex 端任务相关事件 |
| `.ai_workflow/scripts/post-merge-sync.sh` | 本地同步脚本 | 合并或 pull 后将本地任务状态同步回 standby |
| `.agents/` | 代理/规则目录 | 存放补充代理定义或辅助规则 |
| `.claude/` | 工具配置目录 | 存放 Claude 侧配置或辅助文件 |
| `QuantProject/` | 项目目录 | 业务/实验项目子目录之一 |
| `heybox/` | 项目目录 | 业务/实验项目子目录之一 |
| `qm-run-demo/` | 项目目录 | 业务/实验项目子目录之一 |
| `run/` | 项目目录 | 运行脚本或任务目录 |
| `subtitle_extractor/` | 项目目录 | 字幕提取相关项目目录 |

---

<!-- PROJECT:SECTION:DATAFLOW -->
## 三、数据生产、存储与流转

当前工作流数据流按以下路径理解：

1. 用户提出需求后，在 `agents.md` 中登记当前任务或保持 standby。
2. 执行端运行时状态写入 `.ai_workflow/runtime_state_<端>.json`。
3. 关键过程事件追加写入 `.ai_workflow/event_log_<端>.jsonl`。
4. 代码或文档改动完成后，先更新 `PROJECT.md` 的变更记录。
5. 若进入 PR 链路，则由远端完成合并后的归档与待机重置；本地通过 `post-merge-sync.sh` 或 git hook 对齐。
6. 历史任务卡归档到 `.ai_workflow/agents_history/`。

---

<!-- PROJECT:SECTION:DEPENDENCIES -->
## 四、关键依赖与影响范围

| 改动文件 | 直接影响 | 潜在级联影响 | 审计关注点 |
| :--- | :--- | :--- | :--- |
| `agents.md` | 当前任务流转与状态机 | 影响审计、PR 校验、待机重置 | 字段是否与模板一致；状态是否可回放 |
| `PROJECT.md` | 项目级审计与变更记录 | 影响后续维护与任务追踪 | 任务完成前是否更新；记录是否完整 |
| `.ai_workflow/runtime_state_cx.json` | 执行端状态识别 | 影响恢复、继续执行、审计对账 | schema 是否正确；状态是否与任务一致 |
| `.ai_workflow/event_log_cx.jsonl` | 执行过程留痕 | 影响问题追踪与合规审计 | 事件是否连续；是否与 agents 台账一致 |
| `.ai_workflow/scripts/post-merge-sync.sh` | 本地待机同步 | 影响 pull 后本地状态是否正确 | 是否已安装到 `.git/hooks/post-merge` |
| `.github/workflows/auto-merge.yml` | 远端自动归档与重置 | 影响 merge 后是否能自动回 standby | 当前仓库需确认该文件已实际推送 |

---

<!-- PROJECT:SECTION:ISSUES -->
## 五、已知问题、风险与技术债务

| 编号 | 类型 | 来源 | 问题描述 | 影响文件 | 优先级 | 状态 | 建议方案 | 代码锚点 |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| WF-001 | 工作流落地 | 当前仓库状态 | `agents.md` 曾使用旧 `[HANDOFF-WRITE]` 模板，不符合 V5.3 长期任务总表结构 | `agents.md` | 高 | 处理中 | 替换为 standby 壳并按新字段运行 | `DEBT[WF-001]` |
| WF-002 | 文档缺口 | 当前仓库状态 | `PROJECT.md` 初始为空，项目总账未建立 | `PROJECT.md` | 高 | 处理中 | 先建立基础台账，再随任务持续更新 | `DEBT[WF-002]` |
| WF-003 | 端侧不完整 | 当前仓库状态 | 当前公开仓库只看到了 `cx` 侧 runtime/event log，`cc` 侧状态文件未见 | `.ai_workflow/` | 中 | 待处理 | 若未来启用 Claude Code 端，再补齐 `cc` 侧文件 | `DEBT[WF-003]` |
| WF-004 | 自动化待确认 | 工作流部署 | 远端自动归档与待机重置链路需要确认 Actions 文件已真正入仓 | `.github/workflows/auto-merge.yml` | 高 | 待处理 | 推送并做一次最小 PR 闭环测试 | `DEBT[WF-004]` |

---

<!-- PROJECT:SECTION:CHANGELOG -->
## 六、变更记录

> 每次任务完成后，必须先更新本节，再提交 docs commit，再 push。

| 日期 | task_id | 执行端 | 最终改动 | 最终有效范围 | 范围变动/新增需求 | 遗留债务 | 审计结果 | 备注 |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| 2026-04-05 | bootstrap-workflow-20260405 | cx | 初始化 `PROJECT.md`，将 `agents.md` 切换到 V5.3 standby 壳 | `agents.md`, `PROJECT.md` | 无 | WF-003, WF-004 | pending | 作为工作流基础设施落地起点 |
| 2026-04-05 | cx-task-workflow-smoke-test-20260405 | cx | 工作流闭环验证：激活任务、修复 `runtime_state_cx.json` 为合法 JSON、同步 `agents.md` 与事件日志 | `agents.md`, `PROJECT.md`, `.ai_workflow/runtime_state_cx.json`, `.ai_workflow/event_log_cx.jsonl` | 无 | WF-003, WF-004 | pending | 本条仅用于验证工作流链路，不是业务功能开发；同时记录 runtime_state 修复事实 |
| 2026-04-05 | cx-task-workflow-smoke-test-20260405-v2 | cx | 第二轮工作流闭环验证：在修复后的 auto-merge 链路上验证 merge 后 standby reset | `agents.md`, `PROJECT.md`, `.ai_workflow/runtime_state_cx.json`, `.ai_workflow/event_log_cx.jsonl` | 无 | WF-003, WF-004 | pending | 本轮仅验证合并后自动归档与重置，不扩展业务范围 |
| 2026-04-05 | cx-task-auto-merge-smoke-20260405-v2 | cx | 极小 smoke test：仅验证 auto-merge review -> merge 链路，不触碰 post-merge-reset | `agents.md`, `PROJECT.md`, `.ai_workflow/event_log_cx.jsonl` | 无 | WF-003, WF-004 | pending | 本轮任务卡与事件日志保持一致，PR 仅做最小无风险变更 |

---

<!-- PROJECT:SECTION:MAINTENANCE -->
## 七、维护规则

- `PROJECT.md` 是项目级总账，`agents.md` 是任务级总账。
- 所有代码任务，无论是 contract 还是 ad-hoc，均应先在 `agents.md` 中有明确状态。
- 修改完成后，必须先更新 `PROJECT.md`，再提交文档状态相关改动，再进行 push 或 PR。
- 若任务从 ad-hoc 升格为正式交付，`PROJECT.md` 的变更记录中必须体现升格结果。
- 若后续启用 Claude Code 端，需要补齐 `.ai_workflow/runtime_state_cc.json` 与 `.ai_workflow/event_log_cc.jsonl`。
