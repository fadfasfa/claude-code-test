# 项目文档 — claude-code-test

<!-- PROJECT:SECTION:OVERVIEW -->
## 一、项目总览

本仓库当前作为多项目混合工作区与 Hextech 工作流落地仓库使用。

目标分为两部分：

1. 作为实际代码与脚本工作区，承载多个子目录与项目内容。
2. 作为 Hextech V5.3 工作流的运行容器，统一管理任务卡、运行状态、事件日志、本地归档与 standby reset。

当前主线已切换为“本地优先工作流”：

1. 默认允许直接在本地 `main` 完成任务并提交。
2. 若执行中判断风险、变更面或回滚成本超出预期，执行端可自行新建临时本地分支，允许多次提交，完成后再手动合并回 `main`。
3. `agents.md`、`runtime_state_cx.json` 与 `event_log_cx.jsonl` 继续承担本地任务留痕职责。
4. 合并后的归档与待机重置由本地脚本处理，不再把 GitHub Actions 作为默认主链路。

以下能力已停用或降级为非当前主流程：

- 基于 GitHub Actions 的 auto-merge
- PR 合并后自动归档并 reset `agents.md`
- 云端 cleanup / archive maintenance

如后续确有需要，可再恢复远端自动化；当前仓库以本地可控链路为准。

---

<!-- PROJECT:SECTION:FILES -->
## 二、文件职责清单

| 文件 | 类型 | 职责 |
| :--- | :--- | :--- |
| `agents.md` | 工作流任务总表 | 当前仓库的任务卡 / 待机壳；记录当前任务状态、执行模式与台账 |
| `PROJECT.md` | 项目总账 | 记录项目总览、关键文件职责、依赖影响、风险与变更记录 |
| `.ai_workflow/runtime_state_cx.json` | 运行状态文件 | 记录 Codex 端本地工作流状态，面向恢复、归档与 standby 判断 |
| `.ai_workflow/event_log_cx.jsonl` | 事件日志 | 记录 Codex 端任务相关真实事件 |
| `.ai_workflow/scripts/post-merge-sync.sh` | 本地同步脚本 | 本地 merge / pull 后归档当前任务卡并将 `agents.md` 同步回 standby |
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
5. 默认可直接在本地 `main` 完成；若发现风险超出预期，再切换到临时本地分支并在完成后手动合并回 `main`。
6. 本地合并或 pull 完成后，由 `post-merge-sync.sh` 或同类本地钩子执行归档与 standby reset。
7. 历史任务卡归档到 `.ai_workflow/agents_history/`。

---

<!-- PROJECT:SECTION:DEPENDENCIES -->
## 四、关键依赖与影响范围

| 改动文件 | 直接影响 | 潜在级联影响 | 审计关注点 |
| :--- | :--- | :--- | :--- |
| `agents.md` | 当前任务流转与待机壳语义 | 影响审计、恢复、standby reset | 字段是否与本地优先流程一致；状态是否可回放 |
| `PROJECT.md` | 项目级审计与变更记录 | 影响后续维护与任务追踪 | 任务完成前是否更新；记录是否完整 |
| `.ai_workflow/runtime_state_cx.json` | 执行端状态识别 | 影响恢复、继续执行、归档对账 | schema 是否稳定；状态是否贴近本地任务生命周期 |
| `.ai_workflow/event_log_cx.jsonl` | 执行过程留痕 | 影响问题追踪与合规审计 | 事件是否真实、连续；是否与 agents 台账一致 |
| `.ai_workflow/scripts/post-merge-sync.sh` | 本地待机同步 | 影响 merge / pull 后本地状态是否正确 | 本地归档与 standby reset 描述是否一致；是否已安装到 `.git/hooks/post-merge` |

---

<!-- PROJECT:SECTION:ISSUES -->
## 五、已知问题、风险与技术债务

| 编号 | 类型 | 来源 | 问题描述 | 影响文件 | 优先级 | 状态 | 建议方案 | 代码锚点 |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| WF-001 | 工作流落地 | 当前仓库状态 | `agents.md` 曾使用旧 `[HANDOFF-WRITE]` 模板，不符合 V5.3 长期任务总表结构 | `agents.md` | 高 | 已缓解 | 维持 standby 壳，并确保文案与本地优先流程一致 | `DEBT[WF-001]` |
| WF-002 | 文档缺口 | 当前仓库状态 | `PROJECT.md` 初始为空，项目总账曾缺失 | `PROJECT.md` | 高 | 已缓解 | 持续维护总账与变更记录 | `DEBT[WF-002]` |
| WF-003 | 端侧不完整 | 当前仓库状态 | 当前公开仓库只看到了 `cx` 侧 runtime/event log，`cc` 侧状态文件未见 | `.ai_workflow/` | 中 | 待处理 | 若未来启用 Claude Code 端，再补齐 `cc` 侧文件 | `DEBT[WF-003]` |
| WF-004 | 云端自动化已停用 | 工作流迁移 | 旧的 PR auto-merge / merge 后 reset / cleanup Actions 已退出当前主流程 | `PROJECT.md`, `agents.md`, `.ai_workflow/` | 中 | 已处理 | 当前以本地脚本与人工合并为准；如未来需要再恢复远端自动化 | `DEBT[WF-004]` |

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
| 2026-04-05 | cx-task-fix-auto-merge-review-path-20260405 | cx | review-triggered auto-merge 前半段修复：补齐 debug 输出并放宽 review-state 门槛以匹配当前 Codex 集成 | `.github/workflows/auto-merge.yml`, `agents.md`, `PROJECT.md`, `.ai_workflow/event_log_cx.jsonl` | 无 | WF-003, WF-004 | pending | 本次仅诊断并修复 review->auto-merge 前半段，不改 post-merge-reset |
| 2026-04-05 | cx-task-maintain-workflow-cleanup-and-comment-merge-20260405 | cx | 工作流维护增强：新增带白名单保护的归档清理，并将 auto-merge 前半段切换为 Codex comment signal 驱动 | `.github/workflows/auto-merge.yml`, `.github/workflows/cleanup-agents-history.yml`, `agents.md`, `PROJECT.md`, `.ai_workflow/event_log_cx.jsonl` | cleanup 白名单、comment signal 驱动 | WF-003, WF-004 | pending | 本轮仅调整工作流维护链路，不改业务目录 |
| 2026-04-05 | cx-task-maintain-workflow-cleanup-and-comment-merge-20260405 | cx | review follow-up：恢复 `pull_request_review` 驱动、补 `statuses: read`、cleanup 改用 archive 文件首次入库的 git 时间，并解决与 `main` 的 `agents.md` 冲突 | `.github/workflows/auto-merge.yml`, `.github/workflows/cleanup-agents-history.yml`, `agents.md`, `PROJECT.md`, `.ai_workflow/event_log_cx.jsonl` | review 反馈修复、可靠时间基准、冲突解除 | WF-003, WF-004 | pending | 本轮不新开 PR，直接在 PR #13 分支继续修复 |
| 2026-04-05 | cx-task-maintain-workflow-cleanup-and-comment-merge-20260405 | cx | cleanup 时间戳 follow-up：将 git add 时间选择从 newest-first 修正为 oldest add entry，避免同名文件重建后年龄被错误重置 | `.github/workflows/cleanup-agents-history.yml`, `agents.md`, `PROJECT.md`, `.ai_workflow/event_log_cx.jsonl` | oldest add timestamp 修复 | WF-003, WF-004 | pending | 本轮仍在 PR #13 分支上做最小补丁，不新增 PR |
| 2026-04-05 | cx-task-maintain-workflow-cleanup-and-comment-merge-20260405 | cx | review guard/no-op follow-up：非白名单 reviewer 改为 skip，不再把 workflow 打成 failure；cleanup 在目录不存在时跳过 git add 并正常 no-op 退出 | `.github/workflows/auto-merge.yml`, `.github/workflows/cleanup-agents-history.yml`, `agents.md`, `PROJECT.md`, `.ai_workflow/event_log_cx.jsonl` | reviewer skip、目录缺失 no-op | WF-003, WF-004 | pending | 本轮继续在 PR #13 分支做最小修复，不新增 PR |
| 2026-04-05 | cx-task-final-auto-merge-verify-20260405 | cx | 最终 auto-merge 验证：仅在 `PROJECT.md` 追加一条安全审计记录，并同步本端任务卡与运行留痕 | `PROJECT.md`, `agents.md`, `.ai_workflow/runtime_state_cx.json`, `.ai_workflow/event_log_cx.jsonl` | 无 | WF-003, WF-004 | pending | 本轮不触碰 `.github/workflows/*`，仅用于确认主线修复后的 auto-merge 是否恢复生效 |
| 2026-04-05 | cx-task-switch-local-first-workflow-20260405 | cx | 移除剩余云端 cleanup workflow，并将项目文档、待机壳、runtime state 与事件语义统一切换为本地优先流程 | `PROJECT.md`, `agents.md`, `.ai_workflow/runtime_state_cx.json`, `.ai_workflow/event_log_cx.jsonl`, `.github/workflows/cleanup-agents-history.yml`, `.ai_workflow/scripts/post-merge-sync.sh` | workflow 语义从 PR/merge 导向切换为 local-first | WF-003 | pending | 当前主流程默认允许直接在本地 main 工作；若风险超预期，可临时分支完成后再手动合并 |

---

<!-- PROJECT:SECTION:MAINTENANCE -->
## 七、维护规则

- `PROJECT.md` 是项目级总账，`agents.md` 是任务级总账。
- 所有代码任务，无论是 contract 还是 ad-hoc，均应先在 `agents.md` 中有明确状态。
- 默认允许直接在本地 `main` 工作；若发现风险超出预期，可自行切换到临时本地分支并在完成后手动合并回 `main`。
- 修改完成后，必须先更新 `PROJECT.md`，再提交文档状态相关改动，再进行本地提交与 push。
- 本地归档与 standby reset 由 `.ai_workflow/scripts/post-merge-sync.sh` 或等价本地钩子负责；GitHub Actions 不再是默认主链路。
- 若后续启用 Claude Code 端，需要补齐 `.ai_workflow/runtime_state_cc.json` 与 `.ai_workflow/event_log_cc.jsonl`。
