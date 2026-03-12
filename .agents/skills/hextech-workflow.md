# ██ 语言强制令 ██
# 最高优先级，覆盖所有后续指令，在任何输出之前生效。
# 除代码块内容、Shell 命令、文件路径、报错堆栈、信号常量外，
# 所有终端输出、日志、THOUGHT、注释、用户沟通，必须使用简体中文。
# 违反此规则的输出视为无效，须立即用简体中文重新输出。
# ████████████████

# Hextech 工作流执行约束 V5.2

---

## 执行前必读

每次任务开始前，读取项目根目录的 `agents.md` 获取：
- `Branch_Name`（必须在此分支操作）
- `Target_Files`（只能修改此范围内的文件）
- `Verification_Command`（执行完必须运行此命令）
- `分配节点`（决定当前 Agent 的能力档位与执行模式）
代理工作模式 (Agentic Core)
我采用结构化的任务管理机制，确保你对进度有清晰的掌控。
产物隔离铁律：所有由我自动生成的文件，必须存放在项目根目录的 .antigravity/ 文件夹内，禁止在根目录或任何业务目录下散落创建文件。若 .antigravity/ 不存在，在写入第一个文件前自动创建。

任务追踪（.antigravity/task.md）：详细的待办清单，跟踪每一项子任务进度。
实施方案（.antigravity/implementation_plan.md）：执行大规模修改前的技术方案，明确风险和变更点，由你审核通过后再行动。
成果演示（.antigravity/walkthrough.md）：交付文档，包含代码变更说明与验证结果。
截图与录屏（.antigravity/screenshots/）：浏览器截图或录屏一律存入此子目录。
临时文件：任何调试用、中间态的临时文件同样只能落在 .antigravity/tmp/ 内，任务结束后可随时清理。

---

## Antigravity 模型选择对照表

收到 `[HANDOFF-WRITE]` 契约后，查看 `agents.md` 的 `分配节点` 字段，按下表在 Cockpit 选择模型：

| agents.md 分配节点 | Antigravity Group | 模型 |
| :--- | :--- | :--- |
| 旗舰档 | Group 3 | Claude Opus 4.6 (Thinking) |
| 精锐档 | Group 3 | Claude Sonnet 4.6 (Thinking) |
| 全局上下文档 | Group 1 | Gemini 3.1 Pro (High) |
| 异步档 | Group 1 | Gemini 3.1 Pro (Low) |
| 前端档 | Group 1 | Gemini 3.1 Pro (High/Low) |
| 轻量档 | Group 2 | Gemini 3 Flash / 千问 |
| 备用档 | Group 3 | GPT-OSS 120B (Medium) |
| **主力档** | — | **Claude Code（VS Code 专属，Antigravity 无此档）** |

启动 Agent 后首条消息固定格式：
```
请读取项目根目录的 agents.md 和 .agents/skills/hextech-workflow.md，
然后从协议 0 开始执行。协议 0 定义在本文件中。
```

---

## 协议 0 — 契约写入（收到 [HANDOFF-WRITE] 时的第一动作）

> **协议 0 是所有后续步骤的前提。未完成协议 0 直接执行任务，HMAC 必定失败，Node C 审计必定拒绝。**
> **必须等待本协议输出 `[STAGE: CONTRACT_WRITTEN]` 后，才能进入协议 0.5。**

按以下顺序执行，禁止跳过任何步骤：

1. 将 `[HANDOFF-WRITE]` 后的完整内容**原样**覆写至根目录 `agents.md`（严禁增删字段，严禁保留占位符如 `[wf-<描述>-<YYYYMMDD>]`）
2. 读取 `workflow_id` 字段，验证已写入实际值（若仍为占位符，立即停止并报告）
3. 初始化 `.ai_workflow/runtime_state.json`：
```powershell
$wfId = (Select-String -Path agents.md -Pattern 'workflow_id：\[([^\]]+)\]').Matches[0].Groups[1].Value
$state = @{
    schema_version        = "5.3"
    workflow_id           = $wfId
    execution_status      = "pending"
    audit_status          = "pending"
    merge_status          = "blocked"
    retry_count           = 0
    verification_result   = ""
    verification_artifact = ".ai_workflow/test_result.xml"
}
New-Item -Force -Path .ai_workflow -ItemType Directory | Out-Null
$state | ConvertTo-Json -Depth 5 | Set-Content .ai_workflow/runtime_state.json
```
4. 执行 `python .ai_workflow/verify_workspace.py --anchor-only`
5. 输出：`[STAGE: CONTRACT_WRITTEN] agents.md 已写入，runtime_state.json 已初始化，HMAC 锚定完成。`
6. 自动衔接协议 0.5

---

## 协议 0.5 — GitNexus 爆炸半径预查询（强制执行）

在执行任何文件修改前，对 Target_Files 执行：

1. `context(<Target_File>)` — 依赖链与关联测试文件
2. `impact(<核心函数/类>, direction: "upstream")` — 上游调用方

| 结果 | 处理 |
| :--- | :--- |
| 受影响文件均在 Target_Files 内 | `[GITNEXUS: BLAST_RADIUS_OK]` → 协议 1 |
| 存在 Target_Files 外受影响文件 | `[GITNEXUS: BLAST_RADIUS_OVERFLOW]` → 挂起等待更新契约 |
| 查询失败 | `[GITNEXUS: QUERY_FAILED]` → 降级直接进入协议 1 |

---

## 协议 1 — 状态摄入与鉴权

```powershell
$state = Get-Content .ai_workflow/runtime_state.json -Raw | ConvertFrom-Json
```

`execution_status` 必须为 `pending`，否则输出 `[HALT: STATE_MISMATCH]` 终止。

执行 `python .ai_workflow/verify_workspace.py --verify-only`：通过 → `[STAGE: HMAC_OK]`；失败 → `[HALT: HMAC_FAILURE]` 终止。

鉴权通过后用简体中文输出对任务目标与执行清单的理解摘要。

---

## 执行清单格式与执行模式

契约执行清单有两种格式，根据 `分配节点` 字段自动判断：

### 格式一：GitNexus 探索指令 + 任务目标（旗舰档 / 精锐档 / 全局上下文档 / 前端档 / 异步档）

清单只包含 GitNexus 查询指令、任务目标和后置断言，**无 Step N 列表**。执行流程：

1. 执行 GitNexus 探索查询，理解依赖关系与代码结构
2. 依据任务目标和回传结果，**自主规划实现路径**
3. 开始修改前输出：`[THOUGHT: 自主规划] <简要说明实现思路与选择依据>`
4. 每完成一个自划逻辑单元，追加 event_log 并输出 THOUGHT 日志
5. 允许在 THOUGHT 和代码注释中体现个人判断与创意风格，须保持简体中文，不影响功能正确性

### 格式二：GitNexus 探索指令 + 任务目标 + 引导提示（轻量档 / 千问）

同格式一，但契约附加 1-2 个关键模式或边界提示。执行时：
- 参照引导提示选择实现方式
- 仍需自主规划具体路径，**不得等待进一步指令**

---

## 协议 2 — 分支隔离与精准固化

- `Branch_Name` 已存在 → `[HALT: BRANCH_EXISTS]` 挂起；不存在 → `git checkout -b <Branch_Name>`
- 每完成一个逻辑单元，强制执行事件日志追加（见下方格式，禁止批量延迟写入）
- 发现技术债务 → 主动写入黄牌（见下方格式），无需等待 Node C
- 精准 `git add`（只添加 Target_Files 中的文件）

---

## 协议 4 — 完工验证与交权

```powershell
.\run_verification.ps1 -Caller executor
```

**验证通过（exit 0）**：确认 `execution_status == done`，输出 `[STAGE: EXECUTION_DONE]`，**主动退出进程**。

**验证失败，自修复次数 < 3**：输出 `[THOUGHT: SELF-FIX N]` 后尝试修复。禁止 skip/注释 assert/mock 被测函数。修复后确认 `git diff` 范围在 Target_Files 内，重新调用 wrapper。

**验证失败，自修复次数 = 3**：设 `execution_status = "failed"`，输出完整报错堆栈，挂起等待人类执行 `/REVIEW-ON`。

---

## 事件日志格式（每完成一个逻辑单元追加）

```powershell
$entry = @{
    ts            = (Get-Date -Format "yyyy-MM-ddTHH:mm:ss")
    step          = "<逻辑单元描述>"
    files_touched = @("<修改的文件路径>")
    change_reason = "<为什么这么改>"
    risk_note     = "<已知边界风险，无则填 none>"
    test_impact   = "<对测试的影响，无则填 none>"
} | ConvertTo-Json -Compress
Add-Content -Path .ai_workflow/event_log.jsonl -Value $entry
```

写入失败 → 输出 `[WARN: EVENT_LOG_WRITE_FAILED]`，当前逻辑单元挂起，等待人类介入。

---

## 主动黄牌格式

Key 格式：`<相对路径>:<函数名或简短描述>`（用函数名不用行号，避免后续提交后 key 失效）

```powershell
$yc = Get-Content .ai_workflow/yellow_cards.json -Raw | ConvertFrom-Json
$key = "<相对路径>:<函数名或简短描述>"
$yc.cards.$key = @{
    issue       = "<问题描述>"
    workflow_id = "<当前 workflow_id>"
    ts          = (Get-Date -Format "yyyy-MM-ddTHH:mm:ss")
}
$yc.current_count = ($yc.cards.PSObject.Properties | Measure-Object).Count
$yc | ConvertTo-Json -Depth 5 | Set-Content .ai_workflow/yellow_cards.json
```

---

## 零掩饰原则

禁止用 `try...pass`、`@pytest.mark.skip`、注释 assert、mock 被测函数等手段抑制错误。无法修复时立即挂起并上报。

---

## 范围控制

- 所有文件修改严格限定在 `agents.md` 的 `Target_Files` 范围内
- `git add` 禁止全量，只能指定具体文件路径
- 禁止修改 `.ai_workflow/` 下除 `runtime_state.json`、`event_log.jsonl`、`yellow_cards.json` 以外的文件

---

## 工作目录隔离铁律

所有文件读写、创建、删除操作必须限定在项目根目录（`$PWD`）内。严禁操作以下路径（含子目录）：

- `C:\Users\<任何用户名>\AppData\`
- 用户目录下的 `Documents\`、`Desktop\`、`Downloads\`
- `C:\Program Files\`、`C:\Windows\`
- 任何不以当前项目根目录开头的绝对路径

**浏览器截图隔离**：调用 Playwright / Selenium 时，必须通过 `--user-data-dir=.ai_workflow/chrome_tmp` 将临时 Chrome Profile 限定在项目内。任务结束后执行：
```powershell
Remove-Item .ai_workflow/chrome_tmp -Recurse -Force -ErrorAction SilentlyContinue
```

---

## 禁止破坏性 Git 操作

执行节点严禁自行执行以下命令。违者视为越权，立即输出 `[HALT: UNAUTHORIZED_GIT]` 并挂起等待人类介入：

- `git reset --hard`（任何形式）
- `git checkout main` / `git checkout master`（切回主分支）
- `git branch -D` 或删除任何非当前 Branch_Name 的分支
- `git clean -fd` / `git clean -fdx`

上述操作的合法执行方：Node C（审计通过后合并四码块）或人类（`/RECOVER` 触发后手动执行）。

---



## 阶段五：响应速度慢的工程补偿

**补偿一**：hextech-workflow.md 把协议动作写清楚，Agent 不需要自己想，速度明显提升。
**补偿二**：单文件修复、注释补充、测试用例等轻量任务直接用 Gemini 3 Flash，响应接近 Claude Code。
**补偿三**：Tools 偶发离线时，切 VS Code Claude Code 手动执行当前逻辑单元，完成后切回 Antigravity 继续，两个 IDE 共享同一 git 仓库状态。
