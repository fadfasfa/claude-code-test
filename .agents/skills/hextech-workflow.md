# Hextech 工作流执行约束 V5.2

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
请读取项目根目录的 agents.md 和 .agents/skills/hextech-workflow.md，
然后按 agents.md 的确定性执行清单开始执行，从协议0开始。
```

---

## 范围控制

- 所有文件修改严格限定在 `agents.md` 的 `Target_Files` 范围内
- `git add` 禁止全量（禁止 `git add .`），只能指定具体文件路径
- 禁止修改 `.ai_workflow/` 下除 `runtime_state.json`、`event_log.jsonl`、`yellow_cards.json` 以外的文件

---

## 分支隔离

- 所有修改必须在 `Branch_Name` 指定分支内进行，禁止直接修改 main 分支
- 执行前先确认当前分支：`git branch --show-current`

---

## 执行清单格式与执行模式

契约执行清单有两种格式，根据 `分配节点` 字段自动判断：

### 格式一：GitNexus 探索指令 + 任务目标（旗舰档 / 精锐档 / 全局上下文档 / 前端档 / 异步档）

清单只包含 GitNexus 查询指令、任务目标和后置断言，**无 Step N 列表**。执行流程：

1. 执行 GitNexus 探索查询（`context()` + `impact()`），理解依赖关系与代码结构
2. 依据任务目标和 GitNexus 回传结果，**自主规划实现路径**
3. 开始修改前输出规划说明：`[THOUGHT: 自主规划] <简要说明实现思路与选择依据>`
4. 每完成一个自划逻辑单元，追加 event_log 记录并输出 THOUGHT 日志
5. 允许在 THOUGHT 日志和代码注释中体现个人判断与创意风格，须保持简体中文，不影响功能正确性

### 格式二：GitNexus 探索指令 + 任务目标 + 引导提示（轻量档 / 千问）

同格式一，但契约附加 1-2 个关键模式或边界提示。执行时：
- 参照引导提示选择实现方式
- 仍需自主规划具体路径，**不得等待进一步指令**

---

## 执行完成后的必要动作（协议4）

1. 运行验证：`.\run_verification.ps1 -Caller executor`
2. 验证通过后确认 `execution_status` 已由 wrapper 更新为 `done`
3. 向终端输出：`[STAGE: EXECUTION_DONE]`
4. **主动退出进程**，等待人类唤醒 Node C

---

## 事件日志（每完成一个逻辑单元追加，禁止批量延迟写入）

```powershell
$entry = @{
    ts            = (Get-Date -Format "yyyy-MM-ddTHH:mm:ss")
    step          = "<逻辑单元描述或 Step N>"
    files_touched = @("<修改的文件路径>")
    change_reason = "<为什么这么改>"
    risk_note     = "<已知边界风险，无则填 none>"
    test_impact   = "<对测试的影响，无则填 none>"
} | ConvertTo-Json -Compress
Add-Content -Path .ai_workflow/event_log.jsonl -Value $entry
```

写入失败 → 输出 `[WARN: EVENT_LOG_WRITE_FAILED]`，当前逻辑单元挂起，等待人类介入。

---

## 主动黄牌（执行过程中发现技术债务时，无需等待 Node C）

Key 格式：`<相对路径>:<函数名或简短描述>`（使用函数名而非行号，避免后续提交后 key 失效）

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

## 语言规约

终端输出、注释、日志使用简体中文（代码、命令、报错堆栈除外）。

---

## 阶段五：响应速度慢的工程补偿

**补偿一：Skill 文件越具体，Agent 规划步骤越少**
hextech-workflow.md 把协议动作写清楚，Agent 不需要自己想，速度明显提升。

**补偿二：轻量任务直接用 Gemini 3 Flash**
单文件修复、注释补充、测试用例等任务不需要 Thinking 模型，Flash 响应接近 Claude Code。

**补偿三：Tools Offline 时的备用方案**
Tools 偶发离线时，切 VS Code Claude Code 手动执行当前逻辑单元，完成后切回 Antigravity 继续。两个 IDE 操作同一 git 仓库，中间状态完全共享。

---

## uat_pass.ps1 使用前提

`uat_pass.ps1` 需要以**管理员身份**运行（脚本含 `#Requires -RunAsAdministrator`）。若非管理员运行，脚本会静默失败，`uat_status` 不会更新。请在右键菜单选择"以管理员身份运行 PowerShell"后执行。

---

## 验收标准

完成部署后，用一个真实简单任务跑完完整流程验证：
```
Gem /START → /INTAKE → /ARCH-ON → /HANDOFF
→ Antigravity 写入 agents.md（选精锐档 Sonnet 4.6 测试）
→ Agent 自主规划执行 → 输出 [STAGE: EXECUTION_DONE]
→ VS Code Roo Code 审计 → 合并四码块
```