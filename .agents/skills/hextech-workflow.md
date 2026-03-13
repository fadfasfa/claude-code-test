# ██ 语言强制令 ██
# 最高优先级，覆盖所有后续指令，在任何输出之前生效。
# 除代码块内容、Shell 命令、文件路径、报错堆栈、信号常量外，
# 所有终端输出、日志、THOUGHT、注释、用户沟通，必须使用简体中文。
# 违反此规则的输出视为无效，须立即用简体中文重新输出。
# ████████████████

# Hextech 工作流执行约束 V5.3

---

## 执行前必读

每次任务开始前，读取项目根目录的 `agents.md` 获取：
- `Branch_Name`（必须在此分支操作）
- `Target_Files`（只能修改此范围内的文件）
- `Verification_Command`（执行完必须运行此命令）
- `分配节点`（决定当前 Agent 的能力档位与执行模式）

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
请读取项目根目录的 agents.md 和 C:\Users\apple\claudecode\.agents\skills\hextech-workflow.md，
然后从协议 0 开始执行。协议 0 定义在本文件中。
```

---

## 协议 0 — 契约写入（收到 [HANDOFF-WRITE] 时第一动作）

> **必须等待本协议输出 `[STAGE: CONTRACT_WRITTEN]` 后，才能进入协议 0.5。**

1. 将 `[HANDOFF-WRITE]` 后的完整内容**原样**覆写至根目录 `agents.md`（严禁增删字段，严禁保留占位符）
2. 验证 `workflow_id` 已写入实际值（若仍为占位符，立即停止并报告）
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

4. **DACL 锁定 agents.md**（不可跳过）：

```powershell
icacls agents.md /deny "*S-1-1-0:(W,D,DC)" | Out-Null
```

5. 输出：`[STAGE: CONTRACT_WRITTEN] agents.md 已写入并锁定，runtime_state.json 已初始化。`
6. 衔接协议 0.5

---

## 协议 0.5 — GitNexus 爆炸半径检查

在执行任何文件修改前：

| 结果 | 处理 |
| :--- | :--- |
| 受影响文件均在 Target_Files 内 | `[GITNEXUS: BLAST_RADIUS_OK]` → 协议 1 |
| 存在 Target_Files 外受影响文件 | `[GITNEXUS: BLAST_RADIUS_OVERFLOW]` → 挂起等待更新契约 |
| 查询失败 | `[GITNEXUS: QUERY_FAILED]` → 降级继续协议 1 |

---

## 协议 1 — 状态检查与任务摄入

```powershell
$state = Get-Content .ai_workflow/runtime_state.json -Raw | ConvertFrom-Json
```

`execution_status` 必须为 `pending`，否则 `[HALT: STATE_MISMATCH]` 终止。

通过后用简体中文输出对任务目标与 Target_Files 的理解摘要，然后**自主规划实现路径**，开始执行。

---

## 执行清单格式与执行模式

### 高档位（旗舰档 / 精锐档 / 全局上下文档 / 前端档 / 异步档）

清单只含任务目标和后置断言，无 Step N 列表。流程：
1. 执行 GitNexus 探索（context + impact），理解依赖结构
2. 自主规划实现路径，输出 `[THOUGHT: 自主规划] <思路>`
3. 每完成一个逻辑单元，追加 event_log 并输出 THOUGHT

### 轻量档 / 千问

契约附加关键模式或边界提示，参照选择实现方式，仍需自主规划，**不等待进一步指令**。

---

## 协议 2 — 分支隔离与执行日志

`Branch_Name` 已存在 → `[HALT: BRANCH_EXISTS]` 挂起；不存在 → `git checkout -b <Branch_Name>`

**每完成一个逻辑单元，强制追加事件日志：**

```powershell
@{ts=(Get-Date -Format "yyyy-MM-ddTHH:mm:ss");step="<描述>";files_touched=@("<路径>");change_reason="<原因>";risk_note="<风险，无则none>"} | ConvertTo-Json -Compress | Add-Content .ai_workflow/event_log.jsonl
```

写入失败 → `[WARN: EVENT_LOG_WRITE_FAILED]` 挂起。

**发现技术债务时主动写入黄牌：**

```powershell
$yc = Get-Content .ai_workflow/yellow_cards.json -Raw | ConvertFrom-Json
$yc.cards."<路径>:<函数名>" = @{issue="<问题>";workflow_id="<wf_id>";ts=(Get-Date -Format "yyyy-MM-ddTHH:mm:ss")}
$yc.current_count = ($yc.cards.PSObject.Properties | Measure-Object).Count
$yc | ConvertTo-Json -Depth 5 | Set-Content .ai_workflow/yellow_cards.json
```

精准 `git add`（只添加 Target_Files 中的文件），commit：`feat/fix: <描述> [wf-id: <workflow_id>]`

---

## 协议 3 — 零掩饰

以下行为全部禁止，无法修复时立即挂起上报：
- `try...pass` / `@pytest.mark.skip` / 注释掉的 assert / mock 被测函数
- 测试函数体仅含 `assert True` / `assert 1 == 1` 等无意义断言
- 在被测逻辑执行前提前 `return`
- 直接覆写 `.ai_workflow/test_result.xml`

---

## 协议 4 — 完工验证与交权

```powershell
.\run_verification.ps1 -Caller executor
```

- exit 0 → `[STAGE: EXECUTION_DONE]`，**主动退出进程**
- exit 非0，自修复 < 3 次 → `[THOUGHT: SELF-FIX N]`，修复后确认 diff 在 Target_Files 内，重跑
- exit 非0，自修复 = 3 次 → `execution_status = "failed"`，输出完整堆栈，挂起

---

## 固定规则

**agents.md 物理只读**：协议 0 完成 DACL 锁定后，文件系统层面无法写入。

**范围控制**：所有修改严格限定在 Target_Files 内；禁止操作 `.ai_workflow/` 下除 `runtime_state.json`、`event_log.jsonl`、`yellow_cards.json` 以外的文件。

**工作目录隔离**：所有操作限定在项目根目录（`$PWD`）内。严禁：`AppData\`、用户 `Documents\Desktop\Downloads\`、`Program Files\`、`C:\Windows\`、任何不以项目根目录开头的绝对路径。

**浏览器截图隔离**：必须指定 `--user-data-dir=.ai_workflow/chrome_tmp`，任务结束后清理：
```powershell
Remove-Item .ai_workflow/chrome_tmp -Recurse -Force -ErrorAction SilentlyContinue
```

**禁止破坏性 Git 操作**（违反 → `[HALT: UNAUTHORIZED_GIT]` 挂起）：
- `git reset --hard` / `git checkout main` / `git checkout master`
- `git branch -D` 或删除任何分支
- `git clean -fd` / `git clean -fdx`
