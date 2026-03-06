# 三层协同架构节点分配表 (V4.4 实战)

## 全局上下文与调度配置
**分配节点**: [ Node A (Claude Opus 4.6) ]
**需求收敛锁**: [已锁定]
**合并冲突策略**: Git 原生暂存区流转
**全局上下文状态**: **[Node C 审计通过]**

[实战任务：资产重定向错误自愈与前端追踪]

---

## 节点职责与任务阶段

### Node A (Claude Code) - 待启动
- **权限边界**：受限于 DACL 锁，严禁修改 `.ai_workflow/` 目录。
- **核心动作**：直接修改工作区代码 -> `git add` 靶点 -> 执行条件 Stash 压栈。

### 👤 人类 (UAT 验收器)
- **验收点**：当状态变为 `[待UAT]` 时，启动服务并故意访问一个不存在的 ID，观察是否显示占位图且控制台有日志输出。

---

## 确定性执行清单与 Git 靶点映射

- [ ] **Step 1: 后端错误兜底重构**
  - **Git Target File**: `run/web_server.py`
  - **指令**：定位到 `/assets/{id}.png` 路由。在 `RedirectResponse` 失败的异常捕获块中，增加一个最后的 `return`，返回 `https://placehold.co/120x120?text=Missing`。

- [ ] **Step 2: 前端调试链路注入**
  - **Git Target File**: `run/static/detail.html`
  - **指令**：在处理头像加载的 `onerror` 状态机中，增加 `console.error('[Asset-Fail] ID:', id)` 逻辑。

---

## 状态流转规约
Node A 完成 `git add` 后，必须检查工作区并更新 `STASH_CREATED` 标记。最后将全局状态修改为 `[待UAT]` 并休眠。