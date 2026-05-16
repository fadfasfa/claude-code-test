# Repo Governance Audit

生成时间：2026-05-16

## Verdict

- SAFE_TO_FIX: yes
- 阻塞原因：无硬阻塞；但当前工作树已有大量既有改动和删除记录，修复必须只做可解释的治理迁移、路径修正和文档索引，不执行 commit / push / branch / git clean / reset。
- 安全边界：本轮未读取或打印 `auth.json`、token、cookie、API key、proxy secret、`local.yaml`、`proxies.json`。

## Stage 0 Snapshot

| 命令 | 结果 |
| :--- | :--- |
| `git rev-parse --show-toplevel` | `C:/Users/apple/claudecode` |
| `git branch --show-current` | `main` |
| `git log -1 --oneline` | `e33ca6e 1` |
| `git status --short --untracked-files=all` | 既有脏树很重：`.agents/skills/*`、入口文件、`docs/workflows/*`、`scripts/workflow/cx-exec.ps1` 已修改；`.codex-exec-apple/**`、`.workflow/**`、`.learnings/LEARNINGS.md`、根临时产物和旧测试路径已标记删除；`docs/learnings/**`、多个 `docs/workflows/**` 和 `run/workflow/reports/**` 为未跟踪。 |

## High Priority Problems

1. `run/workflow/` 仍承载 CC -> CX 任务状态、日志和长期报告，违反“`run/` 不承载 workflow 规则或长期报告”的目标。
2. `scripts/workflow/cx-exec.ps1` 仍把 `$workflowRoot` 写到 `run\workflow`，还会创建 `reports/`；根 delegator 虽正确转发，但 executor 路径不符合 `.state/workflow/` 目标。
3. worktree 策略存在互相冲突：`AGENTS.md` 和 `docs/workflows/01-worktree-policy.md` 同时写有“显性触发”和“写入任务默认先创建 worktree”。这会让 CC/Codex 边界变得不确定。
4. `docs/workflows/repository-layout.md`、`README.md`、`PROJECT.md`、`CLAUDE.md`、`docs/workflows/10-cc-cx-orchestration.md`、`docs/workflows/cc-cx-collaboration.md` 仍引用 `run/workflow/tasks` 作为当前结果路径。
5. `docs/learnings/ERRORS.md` 约 65KB / 579 行，是明显上下文污染风险；根 `.learnings/` 已不存在，但 learning 内容目前仍在 docs 根下一层。
6. `docs/workflows/_cc_cx_flow_probe.md` 是一次性探针，不应继续放在 active workflow 目录。

## Medium Priority Problems

1. `docs/` 根层有多份规则文档（`task-routing.md`、`safety-boundaries.md`、`module-admission.md`、`continuous-execution.md`、`frontend-validation.md`、`playwright-policy.md`），缺少 `docs/index.md` 统一短索引。
2. `docs/workflows/archives/2026-05-14_cc-cx-integration.md` 约 17KB，是历史长文，放在 workflow 子目录下容易被误注入。
3. `scripts/` 缺少顶层 README；`scripts/workflow/` 缺少脚本目录说明。单个 ps1 已有中文块注释，但没有 `.SYNOPSIS` / `.DESCRIPTION` 型 PowerShell help。
4. `.agents/skills/README.md` 只是白名单，缺少“名称 / 作用 / 触发场景 / 使用者 / 是否默认启用”的 inventory。
5. `.claude/README.md` 写“空白占位”，但 `.claude/skills/karpathy-guardrail/SKILL.md` 存在；需要说明 `.claude` 是 Claude Code 专用接口而非当前规则真相源。

## Low Priority / Optional

1. `scripts/git/ccw-*.ps1` 是 legacy compatibility，会创建或清理 Git worktree；不是默认入口，但需要 README 明确“手动调用、非自动触发”。
2. 旧报告中仍会保留 `run/workflow`、`.workflow`、`.codex-exec-apple`、`.learnings` 等历史引用；迁入 archive 后允许作为历史证据存在。
3. `.codex/` 当前为空目录，可保留为 Codex 项目占位，不需要写 repo-local `config.toml`。

## Directory Boundary Findings

| 路径 | 结论 |
| :--- | :--- |
| `.claude/` | Claude Code 专用接口。当前含 `README.md`、`skills/karpathy-guardrail`、空 `worktrees/`。未发现 worktree 自动创建逻辑；README 需要修正为空白占位 + Claude-only 接口说明。 |
| `.codex/` | 空目录。未发现 `config.toml`、instructions 或 worktree 逻辑；可作为 Codex 占位，不承载运行态。 |
| `.agents/` | Codex 仓库级 skill 白名单。7 个 skill 均在白名单或 AGENTS 中有引用；不建议直接删除，建议补 inventory。 |
| `docs/` | 应改成短索引 + `workflows/` + `reference/` + `archive/`。根层长文和历史长文需要下沉。 |
| `scripts/` | 仓库脚本层。`scripts/workflow/` 是当前执行脚本；`scripts/git/` 是 legacy compatibility，不应作为自动 worktree 主控。 |
| `run/` | Hextech 业务运行区，不应承载 workflow 状态、长期报告或 agent 规则。 |
| `.state/` | 应作为本地 workflow 运行态根；`tasks/current/state/archive/logs` 默认 ignored，`reports` 可保留审查报告。 |
| `.learnings/` | 根目录 `.learnings` 当前不存在且 git 状态显示删除；无硬依赖，根目录不应恢复。 |

## Worktree Boundary Finding

| 路径 | 谁使用 | 是否会创建 worktree | 是否自动触发 | 是否应保留 | 归属建议 |
| :--- | :--- | :--- | :--- | :--- | :--- |
| `AGENTS.md` | Codex | 否，规则文本 | 否 | 保留 | 改为唯一显性触发策略 |
| `CLAUDE.md` | Claude Code | 否，规则文本 | 否 | 保留 | 保留 CC 调用边界 |
| `docs/workflows/01-worktree-policy.md` | 人 / agent | 否，策略文本 | 否 | 保留但改名更直观 | 统一为 `docs/workflows/worktree-policy.md` |
| `.agents/skills/superpowers-project-bridge/SKILL.md` | Codex skill | 否，路由说明 | 否 | 保留 | 不得覆盖 canonical policy |
| `scripts/workflow/worktree-start.ps1` | 手动 workflow 脚本 | 是，`-Apply` 时 `git worktree add --detach` | 否，默认 dry-run | 保留 | 唯一受管创建入口 |
| `scripts/workflow/cleanup-worktree.ps1` | 手动 workflow 脚本 | 否，会 remove | 否，需参数 | 保留 | 清理入口，需 dry-run/保护检查 |
| `scripts/git/ccw-new.ps1` | legacy Git helper | 是，`git worktree add -b` | 否 | 暂保留 | 标记 legacy/manual，不做默认入口 |
| `scripts/git/ccw-gc.ps1` | legacy Git helper | 否，会 remove/prune | 否 | 暂保留 | 标记 legacy/manual |
| `.claude/worktrees/` | Claude Code 本地目录 | 未发现脚本逻辑 | 未发现 | 保留或忽略 | Claude-only 占位，不是主控策略 |
| `.codex/` | Codex 占位 | 否 | 否 | 保留 | 不放 worktree 策略或运行态 |

明确结论：

- 是否发现 CC 自动创建 worktree 的证据：未发现自动创建证据。
- 是否发现 Codex 自动创建 worktree 的证据：未发现自动创建证据。
- 是否存在双系统重复创建风险：当前没有自动触发证据，但文档层存在“显性触发”和“写入任务默认先创建”的冲突，容易导致人工或 agent 误解。
- 推荐唯一 worktree 策略：`docs/workflows/worktree-policy.md` 作为 canonical policy；默认不自动创建。只有用户显性触发或 plan 文件显式 `requires_worktree: true` 时，才通过 `scripts/workflow/worktree-start.ps1 -Apply` 创建单一 detached active worktree。

## Context Pollution Risk

| 文件 | 风险 |
| :--- | :--- |
| `docs/learnings/ERRORS.md` | 约 65KB / 579 行，原始错误/learning 日志，极易撑爆上下文。 |
| `docs/workflows/archives/2026-05-14_cc-cx-integration.md` | 约 17KB 历史报告，不应处于 workflow active 层。 |
| `run/workflow/reports/*.md` | 多份验收/探查报告，当前在业务 `run/` 下，容易被误当现行规则。 |
| `run/workflow/tasks/**/result.json` 和日志 | 运行态结果，可能很长，只应在明确调试时读取。 |
| `docs/workflows/repository-layout.md` | 当前仍把 `run/workflow/` 写成现行运行态根，会污染后续决策。 |

## Proposed File Moves

| 原路径 | 新路径 | 原因 | 是否安全 | 是否需要人工确认 |
| :--- | :--- | :--- | :--- | :--- |
| `run/workflow/tasks/` | `.state/workflow/tasks/` | 运行态迁出 `run/` | 是，按用户授权 | 否 |
| `run/workflow/current/` | `.state/workflow/current/` | 运行态迁出 `run/` | 是 | 否 |
| `run/workflow/state/` | `.state/workflow/state/` | 运行态迁出 `run/` | 是 | 否 |
| `run/workflow/archive/` | `.state/workflow/archive/` | 运行态迁出 `run/` | 是 | 否 |
| `run/workflow/reports/*.md` | `docs/archive/reports/` | 长期报告迁出 `run/` | 是 | 否 |
| `docs/workflows/_cc_cx_flow_probe.md` | `docs/archive/reports/_cc_cx_flow_probe.md` | 一次性探针归档 | 是 | 否 |
| `docs/workflows/archives/2026-05-14_cc-cx-integration.md` | `docs/archive/workflows/2026-05-14_cc-cx-integration.md` | 历史长文下沉 | 是 | 否 |
| `docs/workflows/01-worktree-policy.md` | `docs/workflows/worktree-policy.md` | 明确 canonical policy 名称 | 是 | 否 |
| `docs/learnings/LEARNINGS.md` | `docs/reference/learnings/LEARNINGS.md` | 有价值 learning 下沉到 reference | 是 | 否 |
| `docs/learnings/FEATURE_REQUESTS.md` | `docs/reference/learnings/FEATURE_REQUESTS.md` | 有价值小记录下沉 | 是 | 否 |
| `docs/learnings/ERRORS.md` | `docs/archive/learnings-retired/ERRORS.md` | 原始长日志归档，降低上下文污染 | 是 | 否 |
| `docs/*.md` 中非索引规则文档 | `docs/reference/policies/` | docs 根层只保留索引 | 是 | 否 |

## Proposed Deletions

| 路径 | 删除原因 | 是否有引用 | 是否需要人工确认 |
| :--- | :--- | :--- | :--- |
| 空的 `run/workflow/**` 目录 | 迁移后不再承载内容 | 文档会改为 `.state/workflow` | 否，若已空可删除 |
| 根 `.learnings/` | 已迁移/退休，不应恢复 | 当前无硬依赖 | 不删除内容；保持已删除状态 |
| `.codex-exec-apple/**` | 旧仓内 CODEX_HOME 残留 | 当前 git 状态已删除 | 不恢复，不主动清理更多 |
| `.workflow/**` | 旧运行态残留 | 当前 git 状态已删除 | 不恢复，不主动清理更多 |

## Proposed Documentation Fixes

- `README.md`：改为仓库地图 + 短入口；运行态指向 `.state/workflow/`，提示默认读 `docs/index.md`。
- `PROJECT.md`：修正 `run/workflow`、`docs/learnings`、当前文档列表和目录职责。
- `AGENTS.md`：修正 worktree 策略冲突，保留短规则。
- `CLAUDE.md`：修正 CC -> CX 结果路径到 `.state/workflow/tasks/<task_id>/`。
- `.claude/README.md`：说明 Claude-only 接口，不是规则真相源。
- `docs/index.md`：新增短索引，默认入口。
- `docs/workflows/worktree-policy.md`：统一 worktree 策略。
- `docs/workflows/repository-layout.md`：修正 `.state/workflow` 职责。
- `docs/workflows/10-cc-cx-orchestration.md` 和 `cc-cx-collaboration.md`：修正结果路径和报告位置。
- `docs/workflows/work_area_registry.md`：`run/` 回归业务运行区；`.state/workflow/` 作为本地运行态，不作为业务工作区。
- `scripts/README.md`、`scripts/workflow/README.md`：新增脚本层说明。
- `scripts/workflow/cx-exec.ps1` 和测试：修正输出路径，不重建 `run/workflow`。

## Repair Plan

1. 创建 `docs/index.md`、`docs/reference/`、`docs/archive/`、`docs/archive/reports/`、`docs/reference/learnings/`、`docs/archive/learnings-retired/`。
2. 迁移 `run/workflow/tasks/current/state/archive` 到 `.state/workflow/`，迁移 `run/workflow/reports/*.md` 到 `docs/archive/reports/`。
3. 归档 `_cc_cx_flow_probe.md` 和历史 workflow 长文。
4. 将根层 docs 规则文档下沉到 `docs/reference/policies/`，更新 `PROJECT.md` 和 `docs/index.md`。
5. 将 `docs/workflows/01-worktree-policy.md` 统一为 `docs/workflows/worktree-policy.md`，修正文档中的“默认创建 worktree”冲突。
6. 修复 `scripts/workflow/cx-exec.ps1` 输出路径到 `.state/workflow/tasks/<task_id>/`，不创建 `reports/`，DryRun 不调用真实 Codex。
7. 更新 `.gitignore`，忽略 `.state/workflow/tasks/current/state/archive/logs`，不忽略 docs 长期文档。
8. 新增 `scripts/README.md` 和 `scripts/workflow/README.md`，补充 script maintainability 说明。
9. 运行 PowerShell parse check、`cx-exec.ps1 -DryRun`、路径残留检查、git 状态和 diff stat。
