---
name: superpowers-project-bridge
description: 仓库级 Superpowers 方法论桥接；承载 claudecode 的 S/M/L 风险路由、执行流程、验证和收尾边界。
---

# superpowers-project-bridge

本 skill 是 `claudecode` 的完整 S/M/L 执行流程入口。它不 fork、不改写官方 Superpowers，只把官方方法论接入本仓规则。

## trigger

- 用户明确提到 Superpowers。
- 非琐碎代码、脚本、配置或 workflow 实现任务。
- 任务涉及 bugfix、行为变化、TDD、debugging、worktree、planning、review 或 finishing branch。
- 任务涉及 workflow、agent 规则、plugin、hook、proxy、权限链路、仓库结构、关键数据/策略/回测或跨模块修改。

## risk routing

| 级别 | 判定 | 必要流程 |
| :--- | :--- | :--- |
| `S` | 仅纯文档、链接、措辞、非行为性注释等不改变运行结果的修改 | 可在当前工作区完成；检查 diff；运行匹配验证；可本地 commit |
| `M` | 有限范围但改变行为的任务，例如 bug、脚本逻辑、参数、可执行配置、前端交互、解析或爬虫逻辑 | 使用官方 Superpowers；简短设计；隔离 branch/worktree；计划；适用 TDD 或可重复 smoke/integration 验证；阶段 commit |
| `L` | workflow、agent 规则、plugin、hook、proxy、权限链路、仓库结构、关键数据/策略/回测或跨模块修改 | 完整使用官方 Superpowers 的设计、计划、worktree、验证/review、收尾流程 |

任何行为性改动最低为 `M`；一行高风险配置、hook、proxy 或规则改动不得归为 `S`。

## official Superpowers mapping

- `brainstorming`：需求或设计仍不清时使用；用户已明确批准设计时记录批准事实，不重复请求。
- `using-git-worktrees`：`M/L` 默认隔离执行；先检查当前是否已在 linked worktree，再创建。
- `writing-plans`：`M/L` 需要可执行计划；计划可存在对话中，只有用户或官方流程要求时才落盘。
- `test-driven-development`：行为代码和 bugfix 优先 TDD；配置、文档或无测试入口时使用可重复 smoke/integration 验证并说明原因。
- `systematic-debugging`：遇到失败、异常或回归先查根因，再修。
- `verification-before-completion`：提交、push、PR 或声明完成前必须用新鲜验证证据。
- `requesting-code-review`：重大功能、`L` 级任务、merge 前或用户授权时使用 review；工具不可用时报告未完成，不伪造 subagent review。
- `finishing-a-development-branch`：用户未指定收尾方式时保留 merge / push+PR / keep / discard 选择；用户已指定时按授权执行并验证。

## Git and delivery

- agent 可在批准的开发任务中创建本地 feature branch/worktree，并按验证节点执行本地 add/commit。
- 未收到当前任务明确授权时，不主动 push、创建/更新 PR、merge、删除远端分支或丢弃未合并成果。
- 用户明确要求 push、创建/更新 PR、merge、清理指定 branch/worktree 时，该指令本身即授权；agent 必须自行执行必要命令并验证结果，不得再次要求业务层确认，不得退回手工命令。
- 若底层 sandbox/approval UI 强制一次批准，可发起；获批后继续完成整条链路。
- `force push`、`reset --hard`、删除/丢弃未合并成果、覆盖远端历史，必须被用户明确点名。
- 官方 discard 若自带强制 typed confirmation，保持官方流程；本仓不改官方源码。

## boundaries

- 仍以 `AGENTS.md`、`docs/workflows/work_area_registry.md` 和用户当前任务范围约束写入。
- 不触碰凭据、token、cookie、auth、API key、proxy secret、`.env`、`auth.json`、`local.yaml`、`proxies.json`。
- 不修改 `kb`；不修改 `QuantProject` 业务、策略、数据或回测逻辑，除非用户单独点名。
- 不恢复 CC-CX guard、plan-gate、状态机、command、hook、memory、learning promotion、自动 PR shipping、task resume 或高权限 worktree skill。
- 官方 Superpowers 在 linked worktree merge/cleanup 场景有公开问题和修复记录：`https://github.com/obra/superpowers/issues/940`、`https://github.com/obra/superpowers/issues/999`、`https://github.com/obra/superpowers/issues/238` 与 `https://github.com/obra/superpowers/releases`。本仓不 fork 官方源码；本轮不验收 merge/cleanup 路径，后续实际启用前必须专门验证。

## verification expectation

- 收尾前运行最小有效验证，并说明验证命令、结果和未验证点。
- 规则任务至少运行冲突文本检索、`git diff --check`、目标 skill 可发现性验证、sandbox smoke。
- 远端交付任务必须验证远端 SHA 与本地 SHA 一致；未完成 wrapper/Plugin 链路验收时必须明确报告。
