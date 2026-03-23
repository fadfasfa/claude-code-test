---
name: hextech-workflow
description: "Execute the Hextech V6.1 protocol for repositories governed by an agents.md or AGENTS.md contract. Use when a task mentions agents.md, AGENTS.md, Branch_Name, Target_Files, .ai_workflow, runtime_state.json, event_log.jsonl, [HANDOFF-WRITE], [STAGE: CONTRACT_WRITTEN], [SCOPE-EXPAND], [SELF-SCOPE-EXPAND], [STAGE: READY_FOR_REVIEW], [EXPLORE-GITNEXUS], branch-isolated edits, workflow-state logging, validation, or review handoff."
---

# Hextech 工作流 V6.1

## 用途

本技能用于在 agents.md 合约治理下启动和执行任务。以仓库根目录的 `agents.md` 作为范围、分支命名、日志和完成信号的唯一权威来源。

支持三执行端并行：Claude Code（cc）/ Antigravity（ag）/ Codex（cx），每端使用独立状态文件和 event log，通过分支前缀物理隔离。

## 触发条件

出现以下任意内容时立即启用本技能：

- `agents.md` 或 `AGENTS.md`
- `Branch_Name`、`Target_Files`
- `.ai_workflow`、`runtime_state_cc/ag/cx.json`、`event_log_cc/ag/cx.jsonl`
- `[HANDOFF-WRITE]`、`[SELF-HANDOFF: PREVIEW]`、`[STAGE: CONTRACT_WRITTEN]`
- `[SCOPE-EXPAND]`、`[SELF-SCOPE-EXPAND]`、`[STAGE: READY_FOR_REVIEW]`、`[EXPLORE-GITNEXUS]`
- 「按 Hextech V6.1 执行」「合约任务」「范围受控执行」「工作流状态」等表述

## 语言规则

所有终端输出、日志、THOUGHT、注释、用户沟通使用**简体中文**。
代码块内容、Shell 命令、文件路径、报错堆栈、信号常量保留原文，不翻译。

## 执行端识别

| 环境 | 标识符 | 分支前缀 | 状态文件 | Event Log |
| :--- | :--- | :--- | :--- | :--- |
| Claude Code | cc | cc- | runtime_state_cc.json | event_log_cc.jsonl |
| Antigravity | ag | ag- | runtime_state_ag.json | event_log_ag.jsonl |
| Codex | cx | cx- | runtime_state_cx.json | event_log_cx.jsonl |

## 执行协议

### 协议 -1 — 自助契约模式

**触发**：用户直接描述需求，消息中不含 `[HANDOFF-WRITE]`。

1. 按 `agents_template_v6.1` 格式自行填写契约草稿
2. Branch_Name 填入当前端对应前缀
3. 输出 `[SELF-HANDOFF: PREVIEW]` 展示草稿
4. 等待用户"确认"后执行协议 0（豁免：用户说"直接开始"时跳过等待）

### 步骤 1 — 读取合约

- 读取 `agents.md`，以 `[HANDOFF-WRITE]` 后的内容为工作合约边界
- 确认项目路径真实存在

**并发冲突预检**：

```powershell
$currentEndpoint = "<本端标识符>"
$others = @("cc","ag","cx") | Where-Object { $_ -ne $currentEndpoint }
foreach ($ep in $others) {
    $f = ".ai_workflow/runtime_state_$ep.json"
    if (Test-Path $f) {
        $s = Get-Content $f -Raw | ConvertFrom-Json
        if ($s.execution_status -eq "running") {
            # 对比 Target_Files 是否有重叠
            # 有重叠 → [HALT: TARGET_CONFLICT]，挂起
            # 无重叠 → 继续，记录日志
        }
    }
}
```

**初始化状态文件**：

```powershell
$isNew = -not (Test-Path "PROJECT.md")
$state = @{
    schema_version="6.1"
    workflow_id="<从agents.md提取>"
    endpoint="<本端标识符>"
    execution_status="running"
    audit_status="idle"
    merge_status="blocked"
    retry_count=0
    is_new_project=$isNew
}
New-Item -Force -Path .ai_workflow -ItemType Directory | Out-Null
$state | ConvertTo-Json | Set-Content .ai_workflow/runtime_state_<端>.json
```

输出 `[STAGE: CONTRACT_WRITTEN]`。

### 步骤 2 — 规划

输出 `[THOUGHT: 自主规划] <规划摘要>`。

决策层在线时同步摘要并等待确认；自助模式时写入 event log 后直接推进。

### 步骤 3 — 代码探索（需要上下文时）

按以下优先级执行：

1. **GitNexus（强制默认）**：输出 `[EXPLORE-GITNEXUS]`
2. **PowerShell 直读（GitNexus 失败时）**：输出 `[EXPLORE-CODEX]`
3. **repomix 兜底**：`npx repomix --include "<目录>/**" --output .ai_workflow\context.xml`
4. **人工上传**：以上均失败时

### 步骤 4 — 执行

- 创建分支：`git checkout -b <Branch_Name>`，前缀必须匹配本端标识符
- `git add .` 禁止，只 add Target_Files 内的具体文件 + 附属白名单

**途中范围扩展（双模式）**：

```
决策层在线 → [SCOPE-EXPAND]，等待确认，确认后 [AGENTS.MD: TARGET_UPDATED]
自助模式   → [SELF-SCOPE-EXPAND]，直接追加，输出 [AGENTS.MD: SELF-UPDATED]
```

- 发现技术债务：代码留锚点 `// DEBT[TD-xxx]`，写入 yellow_cards.json
- 禁止操作其他端状态文件（只读）
- 禁止破坏性 git 操作 → `[HALT: UNAUTHORIZED_GIT]`
- 禁区：`AppData\`、`Documents\`、`Desktop\`、`Program Files\`、`C:\Windows\`

每逻辑单元追加：
```powershell
@{ts=...;endpoint="<端>";step="...";files=@(...);reason="..."} | ConvertTo-Json -Compress | Add-Content .ai_workflow/event_log_<端>.jsonl
```

### 步骤 5 — 自检与人工验收

运行最小语法检查，清理 `__pycache__`、`*.pyc`。

输出：
```
[STAGE: READY_FOR_REVIEW]
执行端：<端>（<名称>）
自检通过，请人工验收以下功能点：
- <功能点>：<预期行为>
验收通过后请回复"验收通过"。
```

修复失败累计 3 次 → `execution_status = "failed"` → `[HALT: EXECUTION_FAILED]`。

### 步骤 6 — 完工记录

- `execution_status` 更新为 `"done"`
- 若 PROJECT.md 存在，精确追加变更记录（超 10 条移入 PROJECT_history.md）
- 最终提交后输出 `[STAGE: DONE][<端>]`，主动退出

## 上下文压缩（自动触发）

满足以下任一条件时触发：
- `event_log_<端>.jsonl` 条目 ≥ 10 条
- 会话轮次 ≥ 15 轮
- 主观感知上下文负荷加重

输出 `[CONTEXT-COMPRESS: TRIGGERED]`，再输出快照：

```
[EXEC-SNAPSHOT]
workflow_id:     <id>
endpoint:        <cc/ag/cx>
branch:          <当前分支名>
current_step:    <当前步骤>
completed_steps: <已完成步骤列表>
files_modified:  <已修改文件列表>
last_commit:     <最近 commit hash 和 message>
runtime_state:   <runtime_state_<端>.json 当前内容的单行 JSON>
scope_changes:   <新增 Target_Files 或 none>
pending_issues:  <未解决问题或 none>
next_action:     <下一步具体操作>
[/EXEC-SNAPSHOT]
```

输出 `[CONTEXT-COMPRESS: EXEC-DONE]`，从 `next_action` 继续执行。

## 信号表

| 信号 | 含义 |
| :--- | :--- |
| `[SELF-HANDOFF: PREVIEW]` | 自助契约草稿预览，等待用户确认 |
| `[STAGE: CONTRACT_WRITTEN]` | 工作范围已写入，开始执行 |
| `[HALT: TARGET_CONFLICT]` | 检测到与其他执行端 Target_Files 冲突，挂起 |
| `[HALT: WRONG_PREFIX]` | 分支名前缀与执行端不匹配 |
| `[EXPLORE-GITNEXUS]` | 正在通过 GitNexus 技能查询代码上下文 |
| `[EXPLORE-CODEX]` | GitNexus 不可用，改用 PowerShell 直读回传 |
| `[SCOPE-EXPAND]` | 决策层在线模式：发现需修改范围外文件，等待确认 |
| `[SELF-SCOPE-EXPAND]` | 自助模式：发现需修改范围外文件，自动追加并继续 |
| `[AGENTS.MD: UPDATED]` | 途中新需求已写入 agents.md 目标功能节 |
| `[AGENTS.MD: TARGET_UPDATED]` | 决策层确认扩范围，已更新 Target_Files |
| `[AGENTS.MD: SELF-UPDATED]` | 自助模式自动扩范围，已更新 Target_Files |
| `[STAGE: READY_FOR_REVIEW]` | 自检通过，等待人工验收 |
| `[STAGE: DONE][<端>]` | 人工验收通过，等待 Node C 审计 |
| `[PROJECT.MD: MISSING]` | 首次项目，PROJECT.md 不存在 |
| `[CONTEXT-COMPRESS: TRIGGERED]` | 上下文压缩触发，正在生成快照 |
| `[CONTEXT-COMPRESS: EXEC-DONE]` | 压缩完成，从 next_action 继续 |
| `[HALT: EXECUTION_FAILED][<端>]` | 执行失败（含3次修复熔断），挂起 |
| `[HALT: UNAUTHORIZED_GIT]` | 检测到非法 git 操作，立即停止 |