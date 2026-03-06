## 任务状态机契约 (agents.md)

分配节点：Node B (QwenHaiku 档)
路由决策依据：执行轻量级脚本微调与系统权限自检逻辑，验证 V4.4 链路在增量修改场景下的暂存区覆盖正确性。
全局上下文状态：`[阶段一: 待执行]`

---

### 前置条件 (Pre-conditions)
- HMAC 契约签名：[已核验] (当前处于 Bypass 模式)
- DACL 状态：[上锁（日常模式）]
- pre_merge_snapshot.txt：[已生成] (需继承自上一轮快照)
- STASH_CREATED 标记（路径：`.ai_workflow/STASH_CREATED`）：[继承状态 / 待更新]

### 后置条件 (Post-conditions)
- 成功修订 `run/v44_final_check.py`，新增目录写权限校验逻辑。
- 脚本执行输出包含毫秒级时间戳及 `[V4.4-STAGING-FLOW-RE-CONFIRMED]` 信号。
- 暂存区已同步更新，旧版本代码已被覆盖。

---

### 确定性执行清单

- [x] Step 1: 脚本逻辑增强与权限自检
  - 目标文件：`run/v44_final_check.py`
  - 结构化指令：修改现有的 Python 脚本。
    1. 引入 `os` 模块，使用 `os.access` 检查 `run/` 目录的写权限。若不可写，打印 `[ERROR: RUN-DIR-LOCKED]` 并安全退出。
    2. 使用 `datetime` 模块获取当前时间。打印格式：`YYYY-MM-DD HH:MM:SS.mmm`（毫秒级精度）。
    3. 打印字符串 `[V4.4-STAGING-FLOW-RE-CONFIRMED]`。
  - 风险提示：确保在 `git add` 之前脚本已通过本地运行测试。

---

### Node C 终审记录（由 Node C 填写）
- 审查结论：[Node C 审计通过]
- 当前修复重试次数：[0/5]
- 风险清单摘要：[INFO] 正在执行 UAT 后的需求微调。
- 熔断原因（如有）：无

---

### 冲突避免与底层防线

- 语言规约：执行节点必须唯一使用简体中文进行终端交互。
- 权限隔离：禁止篡改 `.ai_workflow/` 目录下的任何基建脚本。
- 状态机更新铁律（Node B）：修改完成后执行 `git add run/v44_final_check.py`。随后检查工作区，根据是否存在未暂存改动重新刷新 `.ai_workflow/STASH_CREATED` 的 `true/false` 状态。
- 闭环逻辑（Node C）：commit 后读取标记，执行 `git stash pop` 恢复现场。
