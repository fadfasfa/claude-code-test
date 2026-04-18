# 仓库级稳定规则 — Hextech
> version: 7.1-lite
> 本文件只定义稳定规则、角色边界和 review 入口，不承载项目结构说明、任务态、会话态或执行态。

---

## 一、权威分工

- `PROJECT.md`：项目/工作区稳定说明，记录结构、职责、数据流、风险、技术债务与近期变更原则
- `AGENTS.md`：仓库级稳定规则、角色边界与 review 入口
- `CLAUDE.md`：Claude Code 在本仓的根入口提醒
- `agent_tooling_baseline.md`：skill + CLI 基线与能力边界
- `work_area_registry.md`：工作区注册表与新增工作区登记模板
- 审查主输入：`diff`、任务说明、测试结果、必要时的 `PROJECT.md` 同步证据、必要时的 Antigravity 证据

---

## 二、角色边界（vNext）

- Claude / Claude Code：主执行端（默认）；负责任务实现、主链路推进与收口
- Codex：并行独立任务位（可双开选项）；按需并行分担，不再是默认主路径
- Antigravity：高难前端执行端、前端专项审查节点、周期性大型审计角色
- retrieval：独立检索任务，不进入代码任务链路

### 二.A Obsidian 长期知识助手位

- Obsidian 归属 Claude Code 主执行链路的长期知识助手能力
- 在 capability contract 中通过 `required_mcp_groups` 的 `obsidian` 声明接入
- `obsidian` 不作为 Codex 默认依赖；仅在并行任务显式声明时启用

> 已落地能力合同层作为角色翻转过渡层继续保留：
> - `required_bundles`（主字段）
> - `required_mcp_groups`（补充字段）
> - `required_skill_groups`（补充字段）

---

## 三、Bundle 可见性治理语义

- `default_exposed`：`everything-claude-code`
- `explicit_shared`：`shared-validation-docsync`、`shared-retrieval`
- `codex_only`：`codex-execution-review`（Claude 侧停止默认暴露）
- `legacy`：`legacy-compat-bridge`（仅兼容桥接，不作为默认执行前提）

> 治理语义写入规则层，不写入 `.claude/settings*.json` 非运行字段。

---

## 三、审查基线

- 审查只看结果和证据，不围绕旧台账、旧锁态或旧索引发言
- 复杂改动必须同步检查 `PROJECT.md`
- 高风险前端接入必须具备 Antigravity 证据
- 审查不做风格挑刺，只抓实质问题

---

## 四、已退出主流程的机制

- `branch_lock`
- `active_tasks_index`
- `post-merge-sync`
- `finish-task standby reset`
- 根 `agents.md`
- 重型待机壳、恢复壳和大台账式任务壳
- 新增复杂状态机
- 历史 `.agents/*` workflow/contract 文档已从活跃工作树退役；后续如需重构，基于 git history 重建，而不是继续把旧路径当入口

---

## 五、Work-Area Selection And Write Boundaries

- 本仓是多工作区仓；先在 `work_area_registry.md` 确认目标工作区，再开始写入。
- 默认允许跨仓读取；默认只允许在当前目标工作区目录树内写入。
- 未明确目标工作区时，只允许只读探查并列出候选工作区，不得直接写文件。
- 仓库根目录不是默认业务写入区；没有明确仓库治理任务时，不在根目录写业务文件。
- 禁止跨工作区写入；不要为某个工作区任务改写别的工作区。
- 禁止在根目录创建抓取脚本目录、业务目录、输出目录、MCP 配置目录，禁止擅自安装 MCP 或写入 `.mcp.json`。
- 处理某个工作区任务时，从该工作区目录启动工具；Codex 默认使用 `codex --cd <work_area_path>`，默认不要使用 `--add-dir`。
- 当前仓不需要项目级 `.codex/config.toml`；从仓库根启动时，默认只做只读探查或仓库治理，不做业务实现。
