@'
## 任务状态机契约 (agents.md)

workflow_id：[待填写]
contract_version：[待填写]
token_budget：[待填写]
分配节点：[待分配]
路由决策依据：[待填写]
全局上下文状态：[待机]
UAT_Status：[未开始]
Branch_Name：[待填写]
Target_Files：[待填写]

---

### 前置条件 (Pre-conditions)
- HMAC 契约签名：[待核验]
- DACL 状态：[上锁（日常模式）]

### 后置条件 (Post-conditions)
- [待填写]

### Verification_Command
[待填写]

### Verification_Artifact
[待填写]

### Verification_Result
[待填写]

### Thought_Log（事件序列，执行节点每 Step 完成后强制实时写入）
- [待填写]

---

### 确定性执行清单

（待云端下发）

---

### Node C 终审记录（由 Node C 填写）
- 审查结论：[待审查]
- 当前修复重试次数：[0/5]
- 风险清单摘要：[待填写]
- 熔断原因（如有）：无
- UAT_Status 读取值：[未开始 / 已通过]（Node C 只读，不写入）

---

### 冲突避免与底层防线

- 语言规约：执行节点必须唯一使用简体中文（除代码块外）进行终端交互与日志输出。
- 权限隔离（通用）：执行节点禁止修改 DACL 保护范围内的文件（`.git/hooks/*`、`.ai_workflow/`、`Lock-Core.ps1`、`Unlock-Core.ps1`、`run_task.ps1`、`audit_log.txt`）。
- 权限隔离（agents.md 受控豁免）：执行节点仅持有协议 0 完整覆写权和协议 4 原子写入权。Node C 仅持有状态字段正则替换权和修复计数精准替换权。UAT_Status 由人类专属写入。
- 破冰规约：基建文件修改必须由人类启动破冰规约：执行 Unlock-Core.ps1 → git commit --no-verify -m "[INFRA-BYPASS] ..." → Lock-Core.ps1。
- 范围控制：所有 git add 严禁全量，必须指定 Target_Files 中的目标文件。
- 分支隔离：所有代码修改必须在契约指定的任务分支（Branch_Name）内进行，禁止直接修改 main 分支。

### 节点间协议层引用
本契约执行期间，所有节点必须遵守 workflow_v5.0.md 中节点间协议层章节定义的写权限矩阵、阶段交接信号规范与合法状态转移规则。
'@ | Set-Content agents.md