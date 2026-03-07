## 任务状态机契约 (agents.md)

分配节点：[Node A / Node B / Node C]
路由决策依据：[结合能力画像，说明任务特性与节点的匹配逻辑]
全局上下文状态：[意图已锁定待执行 / 待审 / UAT通过待终审 / Node C 审计通过 / CRITICAL 熔断挂起]
Branch_Name：[ai-task-<id>]
Target_Files：[目标文件列表]

---

### 前置条件 (Pre-conditions)
- HMAC 契约签名：[已核验 / 待核验]
- DACL 状态：[上锁（日常模式） / 已解锁（破冰模式）]

### 后置条件 (Post-conditions)
- [任务完成后，必须满足的可观测逻辑结果]

---

### 确定性执行清单

- [ ] Step N: [物理动作描述]
  - 目标文件：[绝对路径]
  - 结构化指令：[非保姆级，描述意图与约束，严禁粘贴完整实现代码]
  - 风险提示：[该步骤的已知边界情况，必填]

---

### Node C 终审记录（由 Node C 填写）
- 审查结论：[通过 / CRITICAL 熔断 / 待审查]
- 当前修复重试次数：[0/5]
- 风险清单摘要：[INFO / WARNING / CRITICAL 条目]
- 熔断原因（如有）：[具体描述]

---

### 冲突避免与底层防线

- 语言规约：执行节点必须唯一使用简体中文（除代码块外）进行终端交互与日志输出。
- 权限隔离：执行节点禁止修改 DACL 保护范围内的文件（`.git/hooks/*`、`.ai_workflow/`、`agents.md`、`Lock-Core.ps1`、`Unlock-Core.ps1`、`run_task.ps1`、`audit_log.txt`）。
- 破冰规约：基建文件修改必须由人类启动破冰规约：执行 `Unlock-Core.ps1` → `git commit --no-verify -m "[INFRA-BYPASS] ..."` → `Lock-Core.ps1`。
- 范围控制：所有 `git add` 严禁全量，必须指定 `Target_Files` 中的目标文件。
- 分支隔离：所有代码修改必须在契约指定的任务分支（`Branch_Name`）内进行，禁止直接修改 main 分支。
