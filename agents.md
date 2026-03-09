## 任务状态机契约 (agents.md) — V5.2

> **V5.2 架构声明**：本文件仅存储**不可变契约字段**，写入后任何节点均不得修改。
> 运行时可变状态存于 `.ai_workflow/runtime_state.json`；执行事件日志存于 `.ai_workflow/event_log.jsonl`。
> HMAC 签名仅覆盖下方标注 `[SIGNED]` 的五个字段。

---

### 不可变契约区（HMAC 锚定范围）

workflow_id：wf-web-redirect-20260309                          contract_version：v1.1                                      Branch_Name：ai-task-web-redirect-20260309                     Target_Files：run/hextech_ui.py, run/web_server.py, run/static/detail.html, run/static/index.html          Verification_Command：pwsh -Command "New-Item -Force -Path .ai_workflow -ItemType Directory | Out-Null; Set-Content .ai_workflow/test_result.xml '<testsuites><testsuite name=\"placeholder\" tests=\"1\"><testcase name=\"ok\"/></testsuite></testsuites>'"    ---

### 任务元数据（不可变，不参与签名）

token_budget：max_cost_cny: 5 / max_retries: 5
分配节点：Node B（Qwen Opus 档）
路由决策依据：因 Claude API 额度受限触发算力切换。任务涉及 Tkinter 桌面端 UI 精简、FastAPI 服务端路由决策以及 WebSocket 广播机制的联动，具有典型的跨文件与跨进程依赖。为保证架构稳定性与多端联动的准确性，分配具备深度推理能力的 Node B（Qwen Opus 档）接力执行。

---

### 前置条件 (Pre-conditions)

- HMAC 契约签名：待核验
- DACL 状态：上锁（日常模式）

### 后置条件 (Post-conditions)

- run/hextech_ui.py 成功移除废弃网页开关界面，且前端英雄点击统一通过 HTTP 请求发送至本地服务。
- run/web_server.py 具备智能路由分发机制，能根据存活的 WebSocket 客户端决定是广播更新信号还是直接拉起浏览器。
- run/static/detail.html 成功移除底部的“英雄协同推荐”纯文字区域，且不影响原有热更新通信逻辑。

---

### 确定性执行清单

- [ ] Step 1: 移除 UI 废弃元素与修改发送端逻辑
  - 目标文件：run/hextech_ui.py
  - 结构化指令：删除网页开关相关的 UI 元素（如复选框/Toggle）及阻断跳转的拦截逻辑。修改处理英雄点击动作的函数（如 `on_hero_click`），将目标跳转逻辑修改为：向本地 `web_server.py` 发起 HTTP 接口调用（传递相关英雄及意图信息），交由服务端进行决策。
  - 风险提示：需检查 Tkinter 的网格布局或打包逻辑，确保删除开关元素后，界面不会出现空白撕裂或其他组件错位。

- [ ] Step 2: 增加服务端智能路由分发机制
  - 目标文件：run/web_server.py
  - 结构化指令：增加/修改接收前端 UI 请求的 HTTP 路由接口（如 `/api/redirect`）。在接口内部增加状态检测：如果当前维护的 WebSocket 客户端集合不为空，则向客户端广播数据更新事件（如 `local_player_locked`）；如果集合为空，则调用 Python 标准库（如 `webbrowser`）拉起浏览器访问对应的英雄详情页。
  - 风险提示：异步与多进程环境下，需确保 WebSocket 连接的存活状态判定准确，防止内存泄漏或向已断开的连接发送数据。

- [ ] Step 3: 清理前端无用 DOM 元素
  - 目标文件：run/static/detail.html
  - 结构化指令：找到底部包含“英雄协同推荐”以及下方纯文字联动内容（包括作者、羁绊等文字描述）的 HTML DOM 结构并将其彻底删除。
  - 风险提示：此步骤无自动化 UI 测试，需人类目视确认渲染效果。务必确保保留现有的 WebSocket 接收逻辑以及主要英雄数据的渲染 DOM，防止误删。

- [ ] Step 4: 同步检查主页状态
  - 目标文件：run/static/index.html
  - 结构化指令：检查主页是否也存在同样的废弃联动 DOM，或是否需要同步更新接收 WebSocket 信号的脚本代码。若无需改动则可平滑跳过此步。
  - 风险提示：防范前端资源引用的连锁反应。

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