# 工作流注册表 — Hextech
> version: 6.0
> 单一事实来源（SSOT）。

---

## § A — 执行端标识与分支前缀

> executor 及分支前缀为追溯元数据，不作为最终审查硬门槛。

| 执行端 | 端标识 | 推荐分支前缀 | 状态文件 | Event Log |
| :--- | :--- | :--- | :--- | :--- |
| Claude Code | `cc` | `cc-task-` | `.ai_workflow/runtime_state_cc.json` | `.ai_workflow/event_log_cc.jsonl` |
| Codex | `cx` | `cx-task-` | `.ai_workflow/runtime_state_cx.json` | `.ai_workflow/event_log_cx.jsonl` |
| 人工 | `human` | `task-` 或任意 | 不适用 | 不适用 |
| 集成 | `int` | `int-task-` | 不适用 | 不适用 |

> 前缀不匹配时审查层仅输出 `[REVIEW-WARN: BRANCH_PREFIX_INFO]`，不阻断。

---

## § B — 附属白名单

```
.ai_workflow/*.json
.ai_workflow/*.jsonl
.ai_workflow/agents_history/*.md
.ai_workflow/scripts/post-merge-sync.sh
PROJECT.md
PROJECT_history.md
.git/hextech/active_tasks_index.json
```

---

## § C — .ai_workflow/runtime_state_<端>.json

```json
{
  "schema_version": "6.0",
  "endpoint": "<cc | cx>",
  "workflow_id": "",
  "task_type": "<code>",
  "execution_status": "idle",
  "retry_count": 0,
  "is_new_project": false,
  "interface_summary": {
    "modified_symbols": [],
    "affected_files": []
  }
}
```

retrieval 任务默认不写 runtime_state。

---

## § E — 三轴路由表

### 1. task_type

| task_type | 含义 |
| :--- | :--- |
| `code` | 代码修改任务 |
| `retrieval` | 检索任务 |

### 2. execution_mode

| execution_mode | 含义 |
| :--- | :--- |
| `contract` | 正式契约任务 |
| `ad-hoc` | 非契约代码任务，仍写 agents.md 任务卡 |

### 3. branch_policy

| branch_policy | 含义 |
| :--- | :--- |
| `required` | 必须新建分支 |
| `on-demand` | 默认不建分支；**仅用户主动要求时**才升格建分支 |
| `none` | 不建分支 |

### 4. completion_mode

| completion_mode | 含义 |
| :--- | :--- |
| `local-main` | 本地 main 直接完成 |
| `local-merge` | 本地分支手动合并后完成 |
| `PR-merge` | 远端 PR merge 后完成 |

### 5. 推荐组合

| 任务场景 | task_type | execution_mode | branch_policy | completion_mode |
| :--- | :--- | :--- | :--- | :--- |
| 正式代码任务 | `code` | `contract` | `required` | `local-merge`（`PR-merge` 仅作为 future integration） |
| 非契约代码任务 | `code` | `ad-hoc` | `on-demand` | `local-main` |
| 检索任务 | `retrieval` | `ad-hoc` | `none` | `local-main` |

---

## § F — 信号

| 信号 | 含义 |
| :--- | :--- |
| `[AGENTS.MD: UPDATED]` | 当前有效范围或台账已更新 |
| `[AGENTS.MD: SELF-UPDATED]` | 自助模式下已同步更新当前有效范围和台账 |
| `[BRANCH: DEFERRED]` | 当前任务默认不建分支（ad-hoc，等待用户主动要求） |
| `[BRANCH: ESCALATED]` | 用户主动要求，已升格建分支 |
| `[BRANCH-BLOCKED: LEASED_BY_<端>]` | 目标分支已被另一执行端租约占用，写入被阻断 |
| `[RISK-WARN: SENSITIVE_CHANGE]` | 检测到敏感路径变更，告知用户但不自动升格分支 |
| `[REVIEW-VERDICT: PASS]` | 最终审查通过（替代旧 `[AUDIT-VERDICT: PASS]`）|
| `[REVIEW-VERDICT: DENY <原因码>]` | 最终审查不通过（替代旧 `[AUDIT-DENY: ...]`）|
| `[REVIEW-WARN: <类型>]` | 审查告警（不阻断）|

---

## § G — Task_Mode 定义

| Task_Mode | 含义 | Antigravity Gate | 集成分支 |
| :--- | :--- | :--- | :--- |
| `standard` | 单端执行 | 双条件触发（见下表） | 否 |
| `dual-end-integration` | 双端都修改 | 是 | 是 |
| `frontend-integration` | Antigravity 生成前端，执行端纳入 | 是 | 否 |

### Gate 触发条件（双条件模型）

`task_mode = standard` 的任务必须**同时满足以下两个条件**才强制进入 Gate Mode：

**条件一（路径命中）** — 当前变更结果（本地 git diff 或 PR diff）包含以下任一路径的修发：

1. **依赖与环境**
   - `**/requirements.txt`, `**/pyproject.toml`, `**/Pipfile*`
   - `**/package.json`, `**/yarn.lock`, `**/package-lock.json`
   - `**/Dockerfile`, `**/.dockerignore`
2. **CI/CD 与工作流配置**
   - `.github/workflows/**`, `.gitlab-ci.yml`
   - `.ai_workflow/**`（排除 `agents_history/*`、`event_log_*.jsonl`）
3. **安全与系统级配置**
   - `**/settings.py`, `**/config.py`, `**/manage.py`
   - `**/.env*`, `**/secrets*`
4. **鉴权与核心扩展**
   - 路径命中 `**/auth/**`, `**/security/**`, `**/permissions/**` 等敏感域
   - 中间件配置、全局路由注册表

**条件二（风险或模式命中）** — 满足以下任一：

- `review_mode = gate`
- 命中安全检测（SEC-00x）
- 涉及鉴权/权限变更

> `dual-end-integration` 和 `frontend-integration` 始终强制 Gate，不适用双条件。

### Gate 豁免（即使路径命中条件一）

以下路径的修发不计入条件一：
- `.ai_workflow/agents_history/*`
- `.ai_workflow/event_log_*.jsonl`
- `.ai_workflow/` 下的 markdown 文档（流程维护）
- `.git/hextech/active_tasks_index.json`

---

## § H — agents.md 状态枚举

| 状态 | 含义 |
| :--- | :--- |
| `standby` | 待机，可复用壳 |
| `active` | 正在执行 |
| `paused` | 因为并发文件重叠被暂停写入 |
| `review-ready` | 已准备进入评审链路（可选，仅 PR 流用到）|
| `done-local` | 本地完成，不进 PR |
| `merged` | 已合并（仅 PR 流用到）|
| `archived` | 已归档 |

### 两条主干完工路径

| 路径 | 状态流转 | completion_mode |
|:---|:---|:---|
| 大任务分支 + 本地合并 | `standby → active → review-ready(可选) → done-local → archived → standby` | `local-merge` |
| 小任务 main 直接完成 | `standby → active → done-local → archived → standby` | `local-main` |
| 大任务分支 + PR *(已退役)* | `standby → active → review-ready → merged → archived → standby` | `PR-merge` |

---

## § I — 本地同步工件

| 工件 | 路径 | 用途 |
| :--- | :--- | :--- |
| post-merge 同步脚本 | `.ai_workflow/scripts/post-merge-sync.sh` | 本地合并后自动对齐 standby |
| git post-merge 钩子（可选） | `.git/hooks/post-merge` | 使脚本在 `git pull` 后自动运行 |
| 全局活跃任务索引 | `.git/hextech/active_tasks_index.json` | 所有未完成任务的 SSOT，用于冲突发现（存放在未跟踪区以支持多分支/worktree 并发） |

安装钩子：
```bash
cp .ai_workflow/scripts/post-merge-sync.sh .git/hooks/post-merge
chmod +x .git/hooks/post-merge
```

---

## § J — 分支锁协议

### 核心规则

**只要某分支已被租约占用（status=leased），另一执行端只能读，不能写。**

### 锁字段定义

```yaml
branch_lock:
  owner: [cc | cx | human | none]       # 当前占有者
  status: [free | leased]               # 锁状态
  acquired_at: [yyyy-MM-ddTHH:mm:ss | none]  # 租约获取时间
  session_id: [uuid | none]             # 防重入标识
```

### 锁操作

| 操作 | 前置条件 | 产生的台账条目 |
|:---|:---|:---|
| `acquire_lock` | `branch_lock.status = free` | `BRANCH_LOCK_ACQUIRED` |
| `release_lock` | `branch_lock.owner = 当前端` | `BRANCH_LOCK_RELEASED` |
| `force_release` | 仅 human 可执行 | `BRANCH_LOCK_FORCE_RELEASED` |

### 执行端行为

1. 建分支时（`branch_policy = required` 或 `on-demand`+升格）：
   - 检查 `branch_lock.status`
   - 若 `free` → acquire_lock，设 owner / session_id / acquired_at
   - 若 `leased` 且 owner ≠ 自己 → `[BRANCH-BLOCKED: LEASED_BY_<other>]`，中止
   - 若 `leased` 且 owner = 自己 且 session_id 相同 → 允许继续（同线程重入）
   - 若 `leased` 且 owner = 自己 但 session_id 不同 → `[BRANCH-BLOCKED: SESSION_CONFLICT]`，视为同执行端不同线程冲突，进入 PAUSE
2. finish-task 时执行 release_lock

### 审查端行为

若 PR diff 中 `branch_lock.owner` 与提交端不一致 → `[REVIEW-VERDICT: DENY BRANCH_CONFLICT]`

---

## § K — 并发控制协议（文件级冲突 SSOT）

### 核心规则

**文件交集即冲突。只要两个活跃任务的 `effective_target_files` 存在交集，后启动的任务必须进入 PAUSE；无文件交集才允许并行。**

> `effective_modified_symbols` 仅作审查参考和追溯说明，不作为并行准入的主条件。两个任务碰到同一文件但不同 symbol，仍然视为冲突。

### 冲突判定优先级

| 优先级 | 条件 | 判定 |
|:---|:---|:---|
| 1 | `effective_target_files` 有交集 | **冲突** — 后任务必须 PAUSE |
| 2 | `effective_target_files` 无交集 | **无冲突** — 允许并行 |
| — | `effective_modified_symbols` 交集 | 仅作审查参考，不单独触发 PAUSE |

### 全局活跃任务索引

路径：`.git/hextech/active_tasks_index.json`
> 注：放在 `.git` 等本地未跟踪区可避免不同分支快照漂移，真实反映本地工作区全状态。

```json
{
  "schema_version": "6.0",
  "active_tasks": [
    {
      "task_id": "<task_id>",
      "status": "<active | paused | review-ready>",
      "branch_name": "<分支名或 none>",
      "executor": "<cc | cx | human>",
      "agents_md_path": "<任务卡的绝对或相对路径>",
      "workspace_path": "<工作区根目录>",
      "effective_target_files": ["file1.py", "file2.py"],
      "effective_modified_symbols": ["file1.py::func_a"],
      "created_at": "<yyyy-MM-ddTHH:mm:ss>",
      "blocked_by_task_id": "<前置任务 ID 或 none>",
      "blocked_on_files": ["<冲突文件列表，或空>"],
      "pause_reason": "<FILE_OVERLAP | SESSION_CONFLICT | MANUAL | none>",
      "resume_condition": "<predecessor_finished | manual | none>"
    }
  ]
}
```

### 新任务启动协议（协议 0 前置步骤）

1. 读取 `.git/hextech/active_tasks_index.json`
2. 计算新任务的 `effective_target_files` 与所有**未完成任务**（即所有已在此索引中，且状态非 `archived` / `standby`，包含 `active`、`paused`、`review-ready`）的交集
3. 若交集非空：
   - 设新任务 `status = paused`
   - 设 `blocked_by_task_id` = 冲突任务的 `task_id`（若多个取最早创建的）
   - 设 `blocked_on_files` = 交集文件列表
   - 设 `pause_reason = FILE_OVERLAP`
   - 设 `resume_condition = predecessor_finished`
   - 在 `execution_ledger` 追加 `PAUSE` 条目（summary 引用 blocked_by_task_id 和冲突文件）
   - 输出 `[TASK-PAUSED: FILE_OVERLAP with <blocked_by_task_id>]`
   - 将新任务写入索引
4. 若交集为空：
   - 设新任务 `status = active`
   - 将新任务写入索引
   - 继续协议 0

### 前任务完成后的恢复协议

finish-task 执行时，除原有步骤外还须：

1. 从 `active_tasks_index.json` 移除当前任务
2. 扫描索引中 `blocked_by_task_id = 当前 task_id` 的任务
3. 对每个被阻塞任务：
   a. 重新计算其与所有剩余未完成任务（包含 active、paused、review-ready）的文件交集
   b. 若不再有冲突 → 设 `status = active`，清空 `blocked_by_task_id` / `blocked_on_files` / `pause_reason`
   c. 根据被阻塞任务的 `workspace_path` 和 `agents_md_path` 定位到其任务文件，并在该任务的 `execution_ledger` 追加 `RESUME` 条目
   d. 输出 `[TASK-RESUMED: <被恢复任务的 task_id>]`
   e. 若仍有冲突（与另一个活跃任务重叠）→ 更新 `blocked_by_task_id` 为新的冲突任务

### 运行中扩范围的冲突重检

协议 2.B（运行中变更）更新 `effective_target_files` 时，还须：

1. 同步更新 `.git/hextech/active_tasks_index.json` 中自己的 `effective_target_files`
2. 重新检查是否与其他未完成任务产生新的文件交集
3. 若产生新冲突并且对方创建早于自己：
   - 必须自动转为 PAUSE（更新 `status=paused`, `blocked_by_task_id` 等）
   - 在 `execution_ledger` 追加 `PAUSE` 条目
   - 输出 `[TASK-PAUSED: RUNTIME_OVERLAP with <task_id>]`，中止执行，等待前序任务完成
