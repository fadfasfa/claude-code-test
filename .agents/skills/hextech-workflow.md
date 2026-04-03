# ██ 语言强制令 ██
除代码块、Shell 命令、文件路径、报错堆栈、信号常量外，
所有终端输出、日志、THOUGHT、注释内容、用户沟通必须使用简体中文。
注释前缀必须使用目标语言的原生注释语法，不得以某种特定符号（如 # 或 //）作为跨语言统一规范。
# ████████████████

# Hextech 工作流 V4.3

> 共享常量、字段所有权、信号定义、枚举、路由：统一见 `workflow_registry_v4.3.md`，本文件不重复定义。
> **agents.md = 不可变初始契约**。运行期所有变动（扩范围、新需求）记入 event_log，不修改 agents.md。

## 执行端识别

端标识、分支前缀、状态文件、Event Log 映射见 `workflow_registry_v4.3.md § A`。合法执行端：`cc` / `cx`。

> **Antigravity 不是执行端**，仅运行 Node C 审计，不参与协议 0-4。

启动后读取本端标识，所有操作使用对应文件，禁止读写其他端的状态文件和日志。

---

## 注释与技术债务标记规范

### 注释规范

- 所有注释内容允许全中文
- 注释前缀必须使用**目标语言的原生注释语法**，不得为了统一而强制使用某种特定符号
  - Python 使用 `#`
  - TypeScript / JavaScript 使用 `//` 或 `/* */`
  - PowerShell 使用 `#`（块注释使用 `<# ... #>`，但禁止以块注释书写意图说明，意图说明用行注释 `#`）
  - 其他语言同理，以各语言标准为准
- 统一的是注释所传达的信息结构（目的、原因、债务标记），而不是注释符号本身

### 技术债务标记规范

执行端采取妥协实现、临时兼容方案或待回收逻辑时，必须在代码中留下债务标记：

- 标记内容统一格式：`DEBT[TD-xxx]: <一句话说明原因>`
- 标记必须置于目标语言合法的注释中（使用该语言原生注释语法）
- 说明内容允许全中文
- 同步写入 `.ai_workflow/yellow_cards.json`（字段见 registry § D）
- 若有 PROJECT.md，精确追加到五节末尾（通过锚点 `<!-- PROJECT:SECTION:ISSUES -->` 定位）

示例（原则说明，不固定注释符号）：

Python 中：`# DEBT[TD-001]: 临时绕过验证，待接口稳定后移除`

TypeScript 中：`// DEBT[TD-002]: 类型断言，上游返回结构待确认`

PowerShell 中：`# DEBT[TD-003]: 硬编码路径，需改为读取配置`

### 验证内容输出规范

验证请求与验证结论属于决策层（DL-Gemini）发起和接收的文本，不属于执行端职责范围。
执行端禁止将决策层验证内容转写为代码块模板输出。若收到涉及验证请求的上下文，按事实日志记录即可，不主动构造验证格式。

---

## 协议 -1 — 自助契约模式

**触发**：用户直接描述需求，消息不含 `[HANDOFF-WRITE]`。

```
1. 按 agents_template_v4.3 格式自行填写契约草稿
   Branch_Name 使用本端前缀（见 registry § A）
   Interface_Summary.modified_symbols 填写本任务涉及的标识符（格式：文件路径::标识符）
   无跨端风险时填 none
   Decision_Validation 区块：自助模式下 Final_Signer 填写 self，Validation_Result 填写 skipped
2. 输出 [SELF-HANDOFF: PREVIEW] 展示草稿全文
3. 等待用户"确认"（用户说"直接开始"则跳过等待）
4. 执行协议 0
```

---

## 协议 0 — 写入工作范围

收到 `[HANDOFF-WRITE]` 或协议 -1 确认后：

### 0.A 并发冲突预检

```powershell
$me = "<本端标识>"  # cc 或 cx

# ① 从 agents.md 解析本端 Interface_Summary.modified_symbols
# 字段格式约定见 workflow_registry_v4.3.md § M
$agentContent = Get-Content "agents.md" -Raw

$msMatch = [regex]::Match($agentContent,
    '(?ms)Interface_Summary:.*?modified_symbols:\s*\n((?:[ \t]+-[ \t]+.+\n?)*)')

# [FIX V-001] regex 失败改为 WARN 输出，不得静默跳过
if (-not $msMatch.Success) {
    Write-Warning "[WARN] agents.md 字段 modified_symbols 解析失败，跳过语义冲突检测。请确认格式符合 workflow_registry_v4.3.md § M 约定。"
}

$mySymbols = if ($msMatch.Success) {
    $msMatch.Groups[1].Value -split "`n" |
        ForEach-Object { $_.Trim() } |
        Where-Object { $_ -match "^-\s+\S" -and $_ -notmatch "^-\s+none" } |
        ForEach-Object { ($_ -replace "^-\s+", "").Trim() }
} else { @() }

# [FIX V-002] 解析本端 Target_Files，用于文件级冲突检测
# 字段格式约定见 workflow_registry_v4.3.md § M
$tfMatch = [regex]::Match($agentContent,
    '(?ms)Target_Files[：:]\s*\n((?:[ \t]+-[ \t]+.+\n?)*)')

if (-not $tfMatch.Success) {
    Write-Warning "[WARN] agents.md 字段 Target_Files 解析失败，跳过文件级冲突检测。请确认格式符合 workflow_registry_v4.3.md § M 约定。"
}

$myTargetFiles = if ($tfMatch.Success) {
    $tfMatch.Groups[1].Value -split "`n" |
        ForEach-Object { $_.Trim() } |
        Where-Object { $_ -match "^-\s+\S" } |
        ForEach-Object { ($_ -replace "^-\s+", "").Trim() }
} else { @() }

foreach ($ep in @("cc","cx") | Where-Object { $_ -ne $me }) {
    $f = ".ai_workflow/runtime_state_$ep.json"
    if (Test-Path $f) {
        $s = Get-Content $f -Raw | ConvertFrom-Json
        if ($s.execution_status -eq "running") {

            # ② [FIX V-002] 文件级冲突检测：比对本端 Target_Files 与对端 affected_files 的交集
            if ($myTargetFiles.Count -gt 0 -and
                $s.interface_summary -and
                $s.interface_summary.affected_files.Count -gt 0) {

                $fileOverlap = $myTargetFiles | Where-Object {
                    $s.interface_summary.affected_files -contains $_
                }
                if ($fileOverlap) {
                    Write-Output "[HALT: TARGET_CONFLICT]"
                    Write-Output "文件级冲突：$($fileOverlap -join ' | ')"
                    Write-Output "对端：$ep（workflow_id: $($s.workflow_id)）"
                    Write-Output "停止执行，等待决策层确认。"
                    return
                }
            }

            # ③ 语义级冲突检测
            if ($mySymbols.Count -gt 0 -and
                $s.interface_summary -and
                $s.interface_summary.modified_symbols.Count -gt 0) {

                $overlap = $mySymbols | Where-Object {
                    $s.interface_summary.modified_symbols -contains $_
                }
                if ($overlap) {
                    Write-Output "[HALT: SEMANTIC_CONFLICT]"
                    Write-Output "重叠 modified_symbols：$($overlap -join ' | ')"
                    Write-Output "对端：$ep（workflow_id: $($s.workflow_id)）"
                    Write-Output "停止执行，等待决策层输出 [SEMANTIC-CONFLICT-RESOLUTION]。"
                    return
                }
            }
        }
    }
}
```

### 0.B 写入状态（仅写本端拥有的字段，见 registry § C 字段所有权表）

```powershell
# 解析 affected_files
$afMatch = [regex]::Match($agentContent,
    '(?ms)Interface_Summary:.*?affected_files:\s*\n((?:[ \t]+-[ \t]+.+\n?)*)')

# affected_files 解析失败时 WARN，不阻断
if (-not $afMatch.Success) {
    Write-Warning "[WARN] agents.md 字段 affected_files 解析失败，affected_files 将置空。请确认格式符合 workflow_registry_v4.3.md § M 约定。"
}

$myAffectedFiles = if ($afMatch.Success) {
    $afMatch.Groups[1].Value -split "`n" |
        ForEach-Object { $_.Trim() } |
        Where-Object { $_ -match "^-\s+\S" } |
        ForEach-Object { ($_ -replace "^-\s+", "").Trim() }
} else { @() }

$isNew = -not (Test-Path "PROJECT.md")

# 字段所有权：execution_status / endpoint / workflow_id / is_new_project / interface_summary 均由执行端写
# audit_status / merge_status 由 Node C 写，此处置初始值
$state = @{
    schema_version    = "4.3"
    endpoint          = $me
    workflow_id       = "<从 agents.md 提取>"
    execution_status  = "running"
    audit_status      = "idle"
    merge_status      = "blocked"
    retry_count       = 0
    is_new_project    = $isNew
    interface_summary = @{
        modified_symbols = @($mySymbols)
        affected_files   = @($myAffectedFiles)
    }
}
New-Item -Force -Path .ai_workflow -ItemType Directory | Out-Null
$state | ConvertTo-Json -Depth 4 | Set-Content ".ai_workflow/runtime_state_$me.json"

# [FIX V-004] 写后读回校验（扩展至 modified_symbols）
$_v = Get-Content ".ai_workflow/runtime_state_$me.json" -Raw | ConvertFrom-Json
if ($_v.execution_status -ne "running") {
    Write-Warning "[WARN] runtime_state 写入校验失败（execution_status），请手动确认后再继续"
}
if ($mySymbols.Count -gt 0 -and $_v.interface_summary.modified_symbols.Count -eq 0) {
    Write-Warning "[WARN] runtime_state 写入校验失败（modified_symbols 丢失），请手动检查后再继续"
}
```

### 0.C PROJECT.md 初始化检查

```powershell
# 若 PROJECT.md 不存在，从模板初始化
if (-not (Test-Path "PROJECT.md")) {
    $templatePath = ".agents/templates/PROJECT_template.md"
    if (Test-Path $templatePath) {
        Copy-Item $templatePath "PROJECT.md"
        Write-Output "[PROJECT.MD: INITIALIZED]"
    } else {
        Write-Output "[PROJECT.MD: MISSING] 模板文件不存在：$templatePath，建议手动创建。"
    }
}
```

新项目时立即创建项目目录。输出 `[STAGE: CONTRACT_WRITTEN]`。

---

## 协议 1 — 自主规划

```
输出 [THOUGHT: 自主规划] <规划摘要>
决策层在线 → 同步摘要，等待确认（超 5 分钟自行推进）
自助模式   → 摘要写入 event_log，直接推进
```

---

## 协议 2 — 执行

**建立分支**：

```powershell
git checkout -b <Branch_Name>
# 前缀必须匹配本端标识（见 registry § A），否则 [HALT: WRONG_PREFIX]
```

**执行循环**（每完成一个逻辑单元）：

```powershell
# 1. 完成文件修改（严格限于 agents.md 中的 Target_Files）

# 2. 追加 event log
@{
    ts       = (Get-Date -Format "yyyy-MM-ddTHH:mm:ss")
    endpoint = $me
    step     = "<描述>"
    files    = @("<路径>")
    reason   = "<原因>"
} | ConvertTo-Json -Compress | Add-Content ".ai_workflow/event_log_$me.jsonl"

# event_log 写后即时校验
$_last = Get-Content ".ai_workflow/event_log_$me.jsonl" | Select-Object -Last 1
try { $_last | ConvertFrom-Json | Out-Null }
catch { Write-Warning "[WARN] event_log 写入校验失败，内容：$_last，请人工核查" }

# 3. git add（仅 Target_Files + 附属白名单，见 registry § B）
foreach ($file in $targetFiles) {
    $p = $file.Trim() -replace '\\', '/'
    if (Test-Path $p) { git add $p }
    else { Write-Warning "[WARN] 文件不存在，跳过 git add: $p" }
}
git add ".ai_workflow/runtime_state_$me.json" ".ai_workflow/event_log_$me.jsonl" 2>$null

# 4. git commit
git commit -m "feat/fix: <描述> [wf-id: <id>][$me]"
```

---

## 途中范围扩展（V4.3 核心变更：agents.md 不再修改）

> **agents.md = 不可变初始契约**。扩范围只写 event_log，不修改 agents.md。

**决策层在线模式**：

```
停止修改范围外文件
输出 [SCOPE-EXPAND] 发现需修改 <文件>，原因：<说明>
等待决策层确认
同意后追加 event_log 条目，输出 [AGENTS.MD: TARGET_UPDATED]（注：仅记录于 event_log）
```

**自助模式**：

```
输出 [SELF-SCOPE-EXPAND] 发现需修改 <文件>，原因：<说明>
必须同步追加一条 event_log 条目（缺任一字段则 Node C 触发 [AUDIT-DENY: UNLOGGED_SCOPE_EXPANSION]）：
```

```powershell
@{
    ts              = (Get-Date -Format "yyyy-MM-ddTHH:mm:ss")
    endpoint        = $me
    step            = "SELF-SCOPE-EXPAND"
    files           = @("<扩展的文件路径>")
    reason          = "SELF-SCOPE-EXPAND: <说明>"
    reason_category = "<bug | dependency | refactor>"   # 合法值见 registry § H
    impact_level    = "<low | medium | high>"            # 合法值见 registry § H
} | ConvertTo-Json -Compress | Add-Content ".ai_workflow/event_log_$me.jsonl"
```

输出 `[AGENTS.MD: SELF-UPDATED] <变更摘要>`，继续执行，无需等待。

---

## 途中新需求

追加 event_log 条目（`step: "NEW_REQUIREMENT"`），输出 `[AGENTS.MD: UPDATED]`，继续执行。

---

## 技术债务

发现技术债务时：

1. 在代码中留下债务标记，格式为 `DEBT[TD-xxx]: <原因>`，置于目标语言原生注释中
2. 注释内容允许全中文，注释符号使用目标语言规范（不统一符号，见本文件开头注释规范）
3. 写入 `.ai_workflow/yellow_cards.json`（字段见 registry § D）
4. 若有 PROJECT.md，精确追加到五节末尾（通过锚点 `<!-- PROJECT:SECTION:ISSUES -->` 定位）

---

## 铁律

- 禁止 `git add .`
- 禁止写入其他端状态文件和日志
- 禁止自主执行破坏性 git 操作（见 registry § G，豁免边界同处说明）
- 禁区：`AppData\` `Documents\` `Desktop\` `Program Files\` `C:\Windows\`
- DACL 保护：`Lock-Core.ps1` / `Unlock-Core.ps1` / `.git/hooks/` 禁止写入
- 注释使用目标语言原生语法；PowerShell 意图说明使用行注释 `#`，禁止以 `<# ... #>` 块注释书写意图说明

---

## 协议 3 — 自检与人工验收

```powershell
python -m py_compile <修改的.py>
# 或 node --check <文件>

Get-ChildItem -Recurse -Include "__pycache__" -Directory | Remove-Item -Recurse -Force -ErrorAction SilentlyContinue
Get-ChildItem -Recurse -Include "*.pyc","*.pyo" | Remove-Item -Force -ErrorAction SilentlyContinue
```

输出：

```
[STAGE: READY_FOR_REVIEW]
执行端：<端>（<名称>）
自检通过，请人工验收以下功能点：
- <功能点>：<预期行为>
验收通过后请回复"验收通过"。
```

- 收到"验收通过" → 协议 4
- 收到问题描述 → 修复；修复次数 < 3 → 重跑自检再出 READY_FOR_REVIEW
- 修复次数 = 3 → `execution_status = "failed"` → `[HALT: EXECUTION_FAILED][<端>]`，输出完整报错，挂起

---

## 协议 4 — 完工记录

```powershell
# 若 PROJECT.md 存在，精确追加六节末尾（只追加新行，禁止重写整节）
# 通过锚点 <!-- PROJECT:SECTION:CHANGELOG --> 定位后在表格末尾追加
$row = "| $(Get-Date -Format 'yyyy-MM-dd') | <workflow_id> | $me | <变更原因> | <变更摘要> | <影响文件> | pending | |"

$lines = (Get-Content "PROJECT.md" | Select-String "^\| \d{4}-\d{2}-\d{2}").Count
if ($lines -gt 10) { <# 移动最旧的 ($lines-10) 条至 PROJECT_history.md #> }

# 只更新本端拥有的字段：execution_status
# 禁止整对象重写（见 registry § C 字段所有权表）
$state = Get-Content ".ai_workflow/runtime_state_$me.json" -Raw | ConvertFrom-Json
$state.execution_status = "done"
$state | ConvertTo-Json -Depth 4 | Set-Content ".ai_workflow/runtime_state_$me.json"

# 写后读回校验
$_v = Get-Content ".ai_workflow/runtime_state_$me.json" -Raw | ConvertFrom-Json
if ($_v.execution_status -ne "done") {
    Write-Warning "[WARN] runtime_state 写入校验失败：请手动将 execution_status 置为 done 后再唤醒 Node C"
}

git add PROJECT.md PROJECT_history.md ".ai_workflow/runtime_state_$me.json"
git commit -m "docs: update PROJECT.md and mark done [<workflow_id>][$me]"
```

若 PROJECT.md 不存在：输出 `[PROJECT.MD: MISSING]`，建议运行 /PROJECT-REVIEW。

输出 `[STAGE: DONE][<端>] 人工验收通过，等待 Node C 审计`，**主动退出进程**。

---

## 上下文压缩（自动触发）

满足任一条件时触发：event_log 条目 ≥ 10 / 会话轮次 ≥ 15 / 主观感知上下文加重。

**禁止在**：等待人工验收期间 / 等待 SCOPE-EXPAND 确认期间 触发。

```
[CONTEXT-COMPRESS: TRIGGERED]
[EXEC-SNAPSHOT]
workflow_id:     <id>
endpoint:        <端>
branch:          <分支名>
current_step:    <当前步骤>
completed_steps: <列表>
files_modified:  <列表>
last_commit:     <hash + message>
runtime_state:   <单行 JSON>
scope_changes:   <event_log 中 SELF-SCOPE-EXPAND 条目数量，或 none>
pending_issues:  <未解决问题或 none>
next_action:     <下一步>
[/EXEC-SNAPSHOT]
[CONTEXT-COMPRESS: EXEC-DONE]
```