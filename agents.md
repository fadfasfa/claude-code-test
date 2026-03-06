# 三层协同架构节点分配表 (V4.4 实战)

## 全局上下文与调度配置
* **分配节点与精细化算力路由**: Node A (Claude 4.6 Opus)
* **需求收敛锁**: 已锁定
* **全局上下文状态**: `[阶段一: 待执行]`
* **实战任务**: 资产重定向错误自愈与前端追踪

---

## 节点职责与任务阶段

### 🤖 Node A (Claude Code) - 当前执行节点
- **权限边界**：受限于 DACL 物理锁，严禁修改 `.ai_workflow/` 目录与基建脚本。仅拥有业务靶点文件的修改权。
- **核心动作**：直接修改工作区代码 -> 精准 `git add <target_files>` -> 执行条件 Stash 压栈 -> 移交交权。

### 👤 人类 (UAT 验收器) - 阶段二
- **验收点**：当状态变为 `[待UAT]` 时，启动本地服务并故意访问一个不存在的资产 ID，观察前端是否正常显示占位图，且浏览器控制台是否输出了追踪日志。

### 👁️ Node C (Roo Code) - 阶段三
- **终审判官**：待人类 UAT 通过后接管。直接对 Git 暂存区执行语义审查。
- **闭环动作**：生成 Commit Message 并执行 `git commit`。随后依据 `STASH_CREATED` 标记执行 `git stash pop`，恢复人类物理现场。

---

## 确定性执行清单与 Git 靶点映射

- [ ] **Step 1: 后端错误兜底重构**
  - **Git Target File**: `run/web_server.py`
  - **指令**: 定位到 `/assets/{id}.png` 路由。在 `RedirectResponse` 失败的异常捕获块中，增加一个最后的 `return` 语句，返回占位图 URL：`https://placehold.co/120x120?text=Missing`。

- [ ] **Step 2: 前端调试链路注入**
  - **Git Target File**: `run/static/detail.html`
  - **指令**: 在处理头像加载的 `onerror` 状态机中，增加 `console.error('[Asset-Fail] ID:', id)` 逻辑，以便在控制台留下追踪线索。

---

## 状态流转规约 (Node A 必读)
1. 上述靶点修改完成后，**严禁全量添加**。必须显式执行 `git add run/web_server.py run/static/detail.html`。
2. 执行 `git status --porcelain` 检查工作区。若有未暂存改动，执行 `git stash push --keep-index -m "AI_WIP"` 并向 `.ai_workflow/STASH_CREATED` 写入 `true`；否则写入 `false`。
3. 将本契约的【全局上下文状态】修改为 `[待UAT]`。
4. 在终端输出完毕信号后，主动断开进程休眠。