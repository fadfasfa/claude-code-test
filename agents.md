## 任务状态机契约 (agents.md) — V5.2

> **V5.2 架构声明**：本文件仅存储**不可变契约字段**，写入后任何节点均不得修改。
> 运行时可变状态存于 `.ai_workflow/runtime_state.json`；执行事件日志存于 `.ai_workflow/event_log.jsonl`。
> HMAC 签名仅覆盖下方标注 `[SIGNED]` 的五个字段。

---

### 不可变契约区（HMAC 锚定范围）

workflow_id：[wf-<描述>-<YYYYMMDD>]                          <!-- [SIGNED] -->
contract_version：[v1.0]                                      <!-- [SIGNED] -->
Branch_Name：[ai-task-<描述>-<YYYYMMDD>]                     <!-- [SIGNED] -->
Target_Files：[目标文件列表，相对路径，禁止绝对路径]          <!-- [SIGNED] -->
Verification_Command：[验证命令，无需附加 --junitxml 参数]    <!-- [SIGNED] -->

---

### 任务元数据（不可变，不参与签名）

token_budget：max_cost_cny: <N> / max_retries: 5
分配节点：[Node A（Opus 档） / Node A（Sonnet 档） / Node B（Sonnet 档） / Node B（Haiku 档） / Node B（Antigravity）]
路由决策依据：[结合能力画像，说明任务分类与节点档位的匹配逻辑]
执行环境：[Claude Code / Antigravity]

---

### 前置条件 (Pre-conditions)

- HMAC 契约签名：[已核验 / 待核验]
- DACL 状态：[上锁（日常模式） / 已解锁（破冰模式）]

### 后置条件 (Post-conditions)

- [任务完成后必须满足的可观测逻辑结果]

---

### 确定性执行清单

> **格式说明**：旗舰档 / 全局上下文档 / 主力档 / 异步档 / 前端档 使用**简洁格式**；轻量档 / 备用档（千问驱动）使用**详细格式**。

**简洁格式（高档位默认）：**

```
[EXPLORE-GITNEXUS]
1. context(<Target_File>) — 依赖、被调用关系、test_files
2. impact(<核心函数/类>, direction: "upstream") — 爆炸半径

任务目标：[一句话描述要达成的可观测结果，不写实现路径]
后置断言：[执行完成后可验证的状态/行为/输出]
```

**详细格式（轻量档 / 备用档专用）：**

- [ ] Step N: [物理动作描述]
  - 目标文件：[相对路径]
  - 结构化指令：[描述意图与约束，包含关键函数名和调用方式]
  - 风险提示：[已知边界情况，必填]

---

### 冲突避免与底层防线

- **agents.md 只读铁律（V5.2）**：本文件写入后，任何节点均不得修改任何字段。运行时状态更新统一写入 `.ai_workflow/runtime_state.json`，事件追加至 `.ai_workflow/event_log.jsonl`。
- **DACL 保护（Lock-Core V5.3）**：`Lock-Core.ps1` / `Unlock-Core.ps1` / `uat_pass.ps1` / `run_verification.ps1` MIC 高完整性保护；`.ai_workflow/audit_log.txt` Deny-Delete + Deny-Overwrite；`.git/hooks/` Deny-Delete。`agents.md` 与 `.ai_workflow/` 目录**不在** Lock-Core V5.3 保护范围内，AI 节点可正常覆写。
- **执行节点写权限**：`runtime_state.json`（execution_status / verification_result / verification_artifact）；`event_log.jsonl`（追加）；`agents.md`（协议0 初始覆写，之后只读）。
- **Node C 写权限**：`runtime_state.json`（audit_status / merge_status / retry_count）。
- **uat_status**：存于 `runtime_state.json`，由 `uat_pass.ps1` 唯一写入（合法值：`not_started / failed / passed`），任何节点均无权直接修改。
- 语言规约：执行节点必须唯一使用简体中文（除代码块外）进行终端交互与日志输出。
- 范围控制：所有 `git add` 严禁全量，必须指定 `Target_Files` 中的目标文件。
- 分支隔离：所有代码修改必须在 `Branch_Name` 指定分支内进行，禁止直接修改 main 分支。
- 破冰规约：基建文件修改必须由人类执行 `Unlock-Core.ps1` → commit --no-verify → `Lock-Core.ps1`。

### 节点间协议层引用

本契约执行期间，所有节点必须遵守 workflow_v5.2.md 中"节点间协议层"章节定义的写权限矩阵、阶段交接信号规范与合法状态转移规则。

---

## 待机态副本（/RECOVER 专用）

> `/RECOVER` 执行时将以下内容覆写至根目录 `agents.md`：
> `Copy-Item agents_idle.md agents.md -Force` 已废弃，改为直接引用本节内容。

```
## 任务状态机契约 (agents.md) — V5.2 待机态

> V5.2 架构声明：本文件仅存储不可变契约字段。运行时状态存于 .ai_workflow/runtime_state.json。
> 当前为待机态，所有字段为占位值，等待云端下发新契约覆写。

### 不可变契约区（HMAC 锚定范围）

workflow_id：[待填写]                    <!-- [SIGNED] -->
contract_version：[待填写]               <!-- [SIGNED] -->
Branch_Name：[待填写]                    <!-- [SIGNED] -->
Target_Files：[待填写]                   <!-- [SIGNED] -->
Verification_Command：[待填写]           <!-- [SIGNED] -->

### 任务元数据（不可变，不参与签名）

token_budget：[待填写]
分配节点：[待分配]
路由决策依据：[待填写]
执行环境：[待填写]

### 前置条件 (Pre-conditions)

- HMAC 契约签名：[待核验]
- DACL 状态：[上锁（日常模式）]

### 后置条件 (Post-conditions)

- [待填写]

### 确定性执行清单

（待云端下发，高档位为 GitNexus 指令+目标格式，轻量档为详细 Step 格式）

### 冲突避免与底层防线

- agents.md 只读铁律（V5.2）：本文件写入后，任何节点均不得修改任何字段。
- 运行时状态更新统一写入 .ai_workflow/runtime_state.json。

### 节点间协议层引用

本契约执行期间，所有节点必须遵守 workflow_v5.2.md 中节点间协议层章节定义的规则。
```