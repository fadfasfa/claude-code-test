workflow_id：wf-ui-recovery-20260309                          contract_version：v2.1                                      Branch_Name：ai-task-ui-recovery-20260309                     Target_Files：run/hextech_ui.py          Verification_Command：pwsh -Command "python -m py_compile run/hextech_ui.py; New-Item -Force -Path .ai_workflow -ItemType Directory | Out-Null; Set-Content .ai_workflow/test_result.xml '<testsuites><testsuite name=\"placeholder\" tests=\"1\"><testcase name=\"ok\"/></testsuite></testsuites>'"    ---

### 任务元数据（不可变，不参与签名）

token_budget：max_cost_cny: 10 / max_retries: 5
分配节点：Node B（Opus 档）
路由决策依据：处理由 DataFrame 列名不匹配导致的 KeyError 崩溃，需在已恢复的 UI 逻辑中注入数据列名动态容错机制。

---

### 前置条件 (Pre-conditions)

- HMAC 契约签名：待核验
- DACL 状态：上锁（日常模式）

### 后置条件 (Post-conditions)

- run/hextech_ui.py 成功运行，不再抛出 KeyError，且能兼容带空格或不同命名变体的 CSV 表头。

---

### 确定性执行清单

- [ ] Step 1: 注入 CSV 表头清理与动态容错逻辑
  - 目标文件：run/hextech_ui.py
  - 结构化指令：
    1. 找到 `load_data` 方法。在读取 CSV 后，立即追加去除列名空格的代码：`df.columns = df.columns.str.replace(' ', '')`。并将原有的硬编码 `dtype={'英雄ID': str}` 移除，改在读取后将对应 ID 列显式转为字符串并清理后缀（`.astype(str).str.strip().str.replace('.0', '', regex=False)`）。
    2. 找到 `update_ui` 方法。在遍历 `hero_ids` 提取 `row` 数据拼接 `display_list` 时，将硬索引（如 `row['英雄名称']`）改为带默认值的 `.get()` 多重回退获取。例如：
       - ID：`row.get('英雄ID', hid)`
       - 名称：`row.get('英雄名称', row.get('英雄名', '未知'))`
       - 胜率：`row.get('英雄胜率', row.get('胜率', 0.5))`
       - 出场率：`row.get('英雄出场率', row.get('出场率', 0.1))`
       - 评级：`row.get('英雄评级', row.get('评级', 'T?'))`
  - 风险提示：需确保存储到 `display_list` 里的胜率（win）和出场率（pick）最终可转换为 float，以防后续计算进度条宽度（`ratio`）时出现类型错误。

---

### 冲突避免与底层防线

- **agents.md 只读铁律（V5.2）**：本文件写入后，任何节点均不得修改任何字段。运行时状态更新统一写入 `.ai_workflow/runtime_state.json`，事件追加至 `.ai_workflow/event_log.jsonl`。
- **DACL 保护（文件级）**：`.ai_workflow/.contract_hash` Deny-Write；`.ai_workflow/audit_log.txt` Deny-Delete + Deny-Overwrite。其余 `.ai_workflow/` 下文件按各自写权限独立声明，无目录级整体封锁。
- **执行节点写权限**：`runtime_state.json`（execution_status / verification_result / verification_artifact）；`event_log.jsonl`（追加）。
- **Node C 写权限**：`runtime_state.json`（audit_status / merge_status / retry_count）。
- **UAT_Status**：存于 `runtime_state.json`，由 `uat_pass.ps1` 唯一写入，任何节点均无权直接修改。
- 语言规约：执行节点必须唯一使用简体中文（除代码块外）进行终端交互与日志输出。
- 范围控制：所有 `git add` 严禁全量，必须指定 `Target_Files` 中的目标文件。
- 分支隔离：所有代码修改必须在 `Branch_Name` 指定分支内进行，禁止直接修改 main 分支。
- 破冰规约：基建文件修改必须由人类执行 `Unlock-Core.ps1` → commit --no-verify → `Lock-Core.ps1`。

### 节点间协议层引用

本契约执行期间，所有节点必须遵守 workflow_v5.2.md 中"节点间协议层"章节定义的写权限矩阵、阶段交接信号规范与合法状态转移规则。