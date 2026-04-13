# Hextech Nexus — 导航索引
> 本文件仅作导航索引，不是权威流程源。
> 权威入口是 `PROJECT.md`、`AGENTS.md`、`workflow_registry.md`、`final_review_contract.md` 与 `codex_review_adapter.md`。

---

## 一、入口速查

- 小任务：直接本地完成
- 大任务：`Plan Draft → Validation Draft → 分支实现 → PR → Codex 自动审查`
- 检索任务：独立链路，不进入代码任务台账
- 前端高风险接入：交给 Antigravity Gate

---

## 二、文件索引

| 文件 | 作用 |
| :--- | :--- |
| `workflow_registry.md` | 常量、路由、信号的 SSOT |
| `PROJECT_template.md` | `PROJECT.md` 结构模板 |
| `agents_template.md` | 临时任务产物 / 历史兼容输出模板 |
| `decision_playbooks.md` | 命令剧本 + SOP |
| `hextech-workflow.md` | 执行端协议概览 |
| `retrieval_workflow.md` | 检索任务独立链路 |
| `final_review_contract.md` | 统一审查合同 |
| `codex_review_adapter.md` | Codex PR 审查适配层 |
| `antigravity_review_contract.md` | Antigravity Gate 审查契约 |

---

## 三、使用顺序

1. 先读 `PROJECT.md`，确认工作区稳定说明
2. 再读 `AGENTS.md`，确认仓库级稳定规则
3. 再读 `workflow_registry.md` 与相关合同/适配层
4. 仅在需要时读 `agents_template.md` 的临时任务产物格式

---

## 四、说明

- `PROJECT.md` 负责项目结构、职责、数据流和技术债务。
- `AGENTS.md` 负责稳定规则、角色边界和 review 入口。
- 本文件只负责导航，不负责签发任务卡或定义权威流程。
