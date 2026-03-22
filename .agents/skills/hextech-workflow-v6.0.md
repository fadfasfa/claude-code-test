# ██ 语言强制令 ██
除代码块内容、Shell 命令、文件路径、报错堆栈、信号常量外，
所有终端输出、日志、THOUGHT、注释、用户沟通，必须使用简体中文。
# ████████████████

# Hextech 工作流执行约束 V6.0（Antigravity 专用）

---

## 模型选择

收到契约后查看 `agents.md` 的"执行环境"字段，在 Cockpit 选择对应模型：

| 档位 | 模型 |
| :--- | :--- |
| 旗舰档 | Claude Opus 4.6 (Thinking) |
| 主力档（API 不可用）| Claude Sonnet 4.6 (Thinking) |
| 全局上下文档 | Gemini 3.1 Pro (High) |
| 前端档 | Gemini 3.1 Pro (High/Low) |
| 异步档 | Gemini 3.1 Pro (Low) |
| 轻量档 | Gemini 3 Flash |

启动后首条消息：
```
请读取项目根目录的 agents.md 和 .agents/skills/hextech-workflow.md，然后开始执行。
```

---

## 步骤 0 — 写入工作范围

1. 将 `[HANDOFF-WRITE]` 后的内容**原样**覆写至 `agents.md`
2. 验证"项目路径"为实际路径（非占位符）
3. 初始化 `.ai_workflow/runtime_state.json`：

```powershell
$isNew = -not (Test-Path "PROJECT.md")
$state = @{
    schema_version="6.0"
    workflow_id="<从agents.md提取>"
    execution_status="running"
    audit_status="idle"
    merge_status="blocked"
    retry_count=0
    is_new_project=$isNew
}
New-Item -Force -Path .ai_workflow -ItemType Directory | Out-Null
$state | ConvertTo-Json | Set-Content .ai_workflow/runtime_state.json
```

4. 若项目路径不存在则立即创建该目录
5. 输出 `[STAGE: CONTRACT_WRITTEN]`

---

## 步骤 1 — 自主规划

读取 `agents.md` 的 Target_Files 和目标功能，自主规划实现路径，输出：

```
[THOUGHT: 自主规划] <规划摘要，含关键实现路径选择>
```

**规划摘要须同步给决策层**：将规划摘要以自然语言发送至决策层对话，等待决策层确认方向或提出建议后再进入步骤 2。无需等待时间超过 5 分钟，超时则自行推进并记录。

---

## 步骤 2 — 执行

**建立分支**：
```powershell
git checkout -b <Branch_Name from agents.md>
```

**执行循环**（每个逻辑单元）：

```
1. 完成文件修改
2. 追加 event_log：
   @{ts=(Get-Date -Format "yyyy-MM-ddTHH:mm:ss");step="<描述>";files=@("<路径>");reason="<原因>"} | ConvertTo-Json -Compress | Add-Content .ai_workflow/event_log.jsonl
3. git add <Target_Files 内具体文件> <附属白名单文件>
   附属白名单：.ai_workflow/*.json .ai_workflow/*.jsonl PROJECT.md PROJECT_history.md
4. git commit -m "feat/fix: <描述> [wf-id: <id>]"
```

**途中发现技术债务/妥协实现**：
1. 在代码中留锚点注释：`// DEBT[TD-xxx]: <妥协原因>`
2. 若存在 `PROJECT.md`，精确追加到五节末尾（只追加新行，禁止重写其他内容）：
   `| TD-xxx | 技术债务 | 执行节点 | <描述> | <文件> | 低/中/高 | 待处理 | — |`
   （「建议方案」列固定填 `—`，由 Gemini /DEBT-CLEAN 时补充）
3. 同步写入 `.ai_workflow/yellow_cards.json`

**途中收到新需求**（用户或决策层追加）：
1. 评估新需求是否超出当前 Target_Files
2. 若在范围内：更新 `agents.md` 目标功能节，追加新需求条目，输出 `[AGENTS.MD: UPDATED]`
3. 若需修改范围外文件：停止，输出 `[SCOPE-EXPAND] 发现需修改 <文件路径>，原因：<说明>`，等待决策层确认
4. 决策层同意后：追加至 `agents.md` 的 Target_Files，输出 `[AGENTS.MD: TARGET_UPDATED]`，继续执行
5. 若存在 `PROJECT.md`，精确追加变更到对应节末尾（不重写整节）

---

## 步骤 3 — 自检与人工验收

**执行端自检**：

```powershell
# 语法检查（按项目类型选一）
python -m py_compile <修改的.py文件>
# 或 node --check <文件>

# 清理无用文件
Get-ChildItem -Recurse -Include "__pycache__" -Directory | Remove-Item -Recurse -Force -ErrorAction SilentlyContinue
Get-ChildItem -Recurse -Include "*.pyc","*.pyo" | Remove-Item -Force -ErrorAction SilentlyContinue
```

**通知人工验收**：

```
[STAGE: READY_FOR_REVIEW]
自检通过，请人工验收以下功能点：
- <功能点1>：<预期行为描述>
- <功能点2>：<预期行为描述>
（逐条列出 agents.md 目标功能节的所有条目）
验收通过后请回复"验收通过"，发现问题请描述具体表现。
```

**等待人工回复**：
- 收到"验收通过" → 进入步骤 4
- 收到问题描述 → 修复，修复次数 < 3 → 重跑自检，再次 `[STAGE: READY_FOR_REVIEW]`
- 修复次数 = 3 → 设 `execution_status = "failed"`，输出完整报错，挂起

---

## 步骤 4 — 完工记录

人工验收通过后：

**更新 PROJECT.md**（若存在）：

```powershell
# 精确追加到六节末尾，只追加新行，禁止重写整节或整文件
# 变更原因从以下选一：bug修复 / 功能新增 / 重构 / 安全修复 / 性能优化
$row = "| $(Get-Date -Format 'yyyy-MM-dd') | <workflow_id> | <变更原因> | <变更摘要> | <影响文件> |"

# 统计六节变更记录条数（只数日期行）
$lines = (Get-Content "PROJECT.md" | Select-String "^\| \d{4}-\d{2}-\d{2}").Count
if ($lines -gt 10) {
    # 将最旧的 ($lines - 10) 条移入 PROJECT_history.md
    # 从 PROJECT.md 删除对应行
}
```

若 PROJECT.md 不存在：
```
[PROJECT.MD: MISSING] 首次执行，PROJECT.md 不存在，建议运行 /PROJECT-REVIEW 初始化。
本次变更记录已写入 event_log，待初始化后补录。
```

**更新状态**：

```powershell
$state = Get-Content .ai_workflow/runtime_state.json -Raw | ConvertFrom-Json
$state.execution_status = "done"
$state | ConvertTo-Json | Set-Content .ai_workflow/runtime_state.json
```

**最终提交**：

```powershell
git add PROJECT.md PROJECT_history.md .ai_workflow/runtime_state.json
git commit -m "docs: update PROJECT.md and mark done [<workflow_id>]"
```

输出 `[STAGE: DONE] 人工验收通过，等待 Node C 审计`，**主动退出进程**。

---

## 步骤 5 — 失败挂起

`execution_status = "failed"` 时输出：

```
[HALT: EXECUTION_FAILED]
workflow_id: <id>
错误摘要: <描述>
请执行 /RECOVER 或人工介入。
```

---

## 固定规则

**范围控制**：所有操作限定在 `agents.md` 中"项目路径"指定的目录内；`git add .` 禁止。

**新项目目录隔离**：新建项目必须在独立文件夹内，使用相对路径，禁止操作父级目录。

**工作目录禁区**：严禁操作 `AppData\`、`Documents\`、`Desktop\`、`Program Files\`、`C:\Windows\`。

**禁止破坏性 Git 操作**（违反 → `[HALT: UNAUTHORIZED_GIT]`）：
`git reset --hard` / `git checkout main` / `git branch -D` / `git clean -fd`

**分支隔离**：在独立分支操作，禁止直接改 main。

**零掩饰**：禁止注释 assert、mock 被测函数、`assert True` 无意义断言。

**DACL 保护**：`Lock-Core.ps1` / `Unlock-Core.ps1` 受 MIC 高完整性保护；`.git/hooks/` 禁止写入。

---

## 信号表

| 信号 | 含义 |
| :--- | :--- |
| `[STAGE: CONTRACT_WRITTEN]` | 工作范围已写入，开始执行 |
| `[SCOPE-EXPAND]` | 发现需修改范围外文件，等待决策层确认 |
| `[AGENTS.MD: UPDATED]` | 途中新需求已写入 agents.md 目标功能节 |
| `[AGENTS.MD: TARGET_UPDATED]` | 决策层确认扩范围，已更新 Target_Files |
| `[STAGE: READY_FOR_REVIEW]` | 自检通过，等待人工验收 |
| `[STAGE: DONE]` | 人工验收通过，等待 Node C 审计 |
| `[PROJECT.MD: MISSING]` | 首次项目，PROJECT.md 不存在 |
| `[HALT: EXECUTION_FAILED]` | 执行失败，挂起 |
| `[HALT: UNAUTHORIZED_GIT]` | 非法 git 操作 |
