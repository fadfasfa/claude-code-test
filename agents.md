## 任务状态机契约 (agents.md) — V5.2

> **V5.2 架构声明**：本文件仅存储**不可变契约字段**，写入后任何节点均不得修改。
> 运行时可变状态存于 `.ai_workflow/runtime_state.json`；执行事件日志存于 `.ai_workflow/event_log.jsonl`。
> HMAC 签名仅覆盖下方标注 `[SIGNED]` 的五个字段。

---

### 不可变契约区（HMAC 锚定范围）

workflow_id：[wf-fix-spider-incomplete-20260309]                          contract_version：[v1.0]                                      Branch_Name：[ai-task-fix-spider-incomplete-20260309]                     Target_Files：[apex_spider.py]          Verification_Command：[pwsh -Command "New-Item -Force -Path .ai_workflow -ItemType Directory | Out-Null; Set-Content .ai_workflow/test_result.xml '<testsuites><testsuite name=\"placeholder\" tests=\"1\"><testcase name=\"ok\"/></testsuite></testsuites>'"]    ---

### 任务元数据（不可变，不参与签名）

token_budget：max_cost_cny: 10 / max_retries: 5
分配节点：[Node B（Qwen Opus 档）]
路由决策依据：[任务涉及爬虫匹配逻辑重构、重试机制引入与多数据源合并，具有一定的逻辑复杂度；用户明确指定不可使用 Claude，故分配至 Node B（Qwen Opus 档）确保执行深度与准确性。]

---

### 前置条件 (Pre-conditions)

- HMAC 契约签名：[待核验]
- DACL 状态：[上锁（日常模式）]

### 后置条件 (Post-conditions)

- 爬虫具备网络请求重试机制，可应对偶发 429/50x 错误。
- 英雄名称匹配采用多字段（name/title/en_name）正则化识别，不再因称呼差异导致遗漏。
- 输出的 JSON 数据中完整包含合并后的 `aliases` 字段，且确保 `aliases` 未被用于网页抓取时的名称匹配。

---

### 确定性执行清单

- [ ] Step 1: 引入网络重试机制
  - 目标文件：apex_spider.py
  - 结构化指令：修改 `ApexSpider.__init__`，为 `self.session` 挂载 `requests.adapters.HTTPAdapter`，配置 `urllib3.util.Retry`（重试 3 次，backoff_factor=0.5，状态码包含 429, 500, 502, 503, 504）。
  - 风险提示：确保挂载 HTTPAdapter 时分别适配 `http://` 和 `https://`。

- [ ] Step 2: 重构英雄名称索引结构与数据合并
  - 目标文件：apex_spider.py
  - 结构化指令：在 `main` 函数加载 `Champion_Core_Data.json` 和 `hero_aliases.json` 后，重构查找字典：
    1. 构建输出用的完整数据字典 `core_info_dict`，包含英雄的所有核心字段并合并对应的 `aliases` 列表。
    2. 构建专用于**网页名称匹配**的 `search_index`。遍历核心数据，将英雄的 `name`, `title`, `en_name` 进行正则化清洗（转小写、去除空格和特殊符号）后作为 Key，映射到英雄 ID。
    3. **严格约束**：不得将 `aliases` 中的缩写加入 `search_index`，以防止匹配污染。
  - 风险提示：两个字典的职责必须严格解耦，一个用于最终数据组装，一个仅用于从网页抓取的杂乱名字中定位 ID。

- [ ] Step 3: 优化网页提取匹配逻辑与并发数
  - 目标文件：apex_spider.py
  - 结构化指令：
    1. 修改 `main` 中遍历 `champions` 列表的逻辑：对网页提取的 `champ_name` 执行与 Step 2 相同的正则化清洗，然后去 `search_index` 中查找对应的英雄 ID。若匹配成功，从 `core_info_dict` 取出完整信息组装任务；若失败则记录到 `skipped_names`。
    2. 将 `ThreadPoolExecutor(max_workers=16)` 下调为 `max_workers=8`，降低因并发过高触发目标站点风控的概率。
  - 风险提示：清洗逻辑需确保健壮性（如 `replace(" ", "").lower()` 等处理），避免 `None` 引用。

---

### 冲突避免与底层防线

- **agents.md 只读铁律（V5.2）**：本文件写入后，任何节点均不得修改任何字段。运行时状态更新统一写入 `.ai_workflow/runtime_state.json`，事件追加至 `.ai_workflow/event_log.jsonl`。
- **DACL 保护（文件级）**：`.ai_workflow/.contract_hash` Deny-Write；`.ai_workflow/audit_log.txt` Deny-Delete + Deny-Overwrite。其余 `.ai_workflow/` 下文件按各自写权限独立声明，无目录级整体封锁。
- **执行节点写权限**：`runtime_state.json`（execution_status / verification_result / verification_artifact）；`event_log.jsonl`（追加）。
- **Node C 写权限**：`runtime_state.json`（audit_status / merge_status / retry_count）。
- **uat_status**：存于 `runtime_state.json`，由 `uat_pass.ps1` 唯一写入（合法值：`not_started / failed / passed`），任何节点均无权直接修改。
- 语言规约：执行节点必须唯一使用简体中文（除代码块外）进行终端交互与日志输出。
- 范围控制：所有 `git add` 严禁全量，必须指定 `Target_Files` 中的目标文件。
- 分支隔离：所有代码修改必须在 `Branch_Name` 指定分支内进行，禁止直接修改 main 分支。
- 破冰规约：基建文件修改必须由人类执行 `Unlock-Core.ps1` → commit --no-verify → `Lock-Core.ps1`。

### 节点间协议层引用

本契约执行期间，所有节点必须遵守 workflow_v5.2.md 中"节点间协议层"章节定义的写权限矩阵、阶段交接信号规范与合法状态转移规则。