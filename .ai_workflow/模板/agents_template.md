## 任务状态机契约 (agents.md)

分配节点：[Node A / Node B / Node C]
路由决策依据：[结合能力画像，说明任务特性与节点的匹配逻辑]
全局上下文状态：[意图已锁定待执行 / 增量代码已暂存 / 待 Node C 终审 / CRITICAL 熔断挂起]

---

### 前置条件 (Pre-conditions)
- HMAC 契约签名：[已核验 / 待核验]
- DACL 状态：[上锁（日常模式） / 已解锁（破冰模式）]
- pre_merge_snapshot.txt：[已生成 / 待生成]
- STASH_CREATED 标记：[true / false / 待写入]

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
- 风险清单摘要：[INFO / WARNING / CRITICAL 条目]
- 熔断原因（如有）：[具体描述]

---

### 冲突避免与底层防线

- 语言规约：执行节点必须唯一使用简体中文（除代码块外）进行终端交互与日志输出。
- 权限隔离：执行节点禁止修改 DACL 保护范围内的文件（`.git/hooks/*`、`verify_workspace.py`、`Lock-Core.ps1`、`run_task.ps1`、`audit_log.txt`、`pre_merge_snapshot.txt`）。
- 破冰规约：基建文件修改必须由人类启动破冰规约：执行 `Unlock-Core.ps1` → `git commit --no-verify -m "[INFRA-BYPASS] ..."` → `Lock-Core.ps1`。
- 范围控制：阶段三 CRITICAL 熔断后，子分支 A 的 `git add` 严禁全量，必须指定 `<target_files>`。
- 状态机闭环：`git stash pop` 执行前必须检查 `STASH_CREATED` 标记，标记为 `false` 时跳过 pop。