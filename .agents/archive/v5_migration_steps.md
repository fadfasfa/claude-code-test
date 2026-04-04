> **[历史迁移文档]**
> 本文件仅说明从 V4.3 迁移到 V5.0 时的过渡思路，**不作为当前规范**。
> 当前规范以 `workflow_registry.md` / `decision_playbooks.md` / `README_workflow.md` 为准。
> 本文件中关于 Antigravity 前端模式的描述（"独立任务，不走 cc/cx 协议"）已过时，
> 当前 `frontend-integration` 任务须走完整代码链路并强制通过 Antigravity Gate Mode 审查。

---

# Hextech V4.3 → V5.0 落地步骤（历史归档）
## Codex PR 审查接替合并门控 · Antigravity 转型为安全扫描 + 前端工具

---

## 新架构一览

```
决策层（DL-Gemini）
    ↓ 契约下发
执行层（cc / cx 双端）
    ↓ [STAGE: DONE] push 分支，开 PR
云端 Codex（自动 PR 审查）← 合并门控，替代原 Node C 的范围+合规职能
    ↓ Approve
人类合并

另开独立窗口（按需，不在主流程路径上）：
  Antigravity A — 安全漏洞扫描（任意时机，针对本地代码）
  Antigravity B — 前端页面制作（独立任务，不走 cc/cx 协议）
```

**和 V4.3 的本质区别**：
- Codex PR 审查负责"这次变更合不合规、能不能合并"——原 Node C 主职
- Antigravity 负责"代码有没有漏洞"和"前端页面怎么做"——两个独立专项，不卡主流程

---

## 阶段 0 — GitHub 仓库配置（必须最先做）

### 0.1 设置 Required Reviewer

1. 仓库 → Settings → Branches → `main` → Edit
2. 勾选 **Require a pull request before merging**
3. 勾选 **Require approvals**，数量设 1
4. Required reviewers 添加 Codex bot 账号
5. 勾选 **Dismiss stale pull request approvals when new commits are pushed**
   （执行端修复后 push，旧 approve 自动失效，强制重审）
6. 保存

验证：开测试 PR，确认合并按钮变灰并提示"Review required"。

### 0.2 确认 Codex 审查触发

截图已确认：智能触发器已启用，仓库已连接，无需额外配置。

---

## 阶段 1 — 创建 CODEX_REVIEW_RULES.md

路径：`.agents/codex_review_rules.md`

这是 Codex 审查时的行为规则，替代原 node_c_prompt 在合规审查上的职能。
当前版本见 `codex_review_rules.md`（v5.1），此处不重复展示。

---

## 阶段 2 — 重新定义 Antigravity 的两个角色

Antigravity 不再卡主流程，改为两个独立专项工具。在 Antigravity 系统提示中替换原 node_c_prompt，分别准备两套提示。

### 角色 A：本地安全漏洞扫描

**用法**：任意时机，不依赖 PR，不依赖执行端状态，想扫就扫。
**触发方式**：人类直接粘贴代码片段或文件路径，或描述"扫描 X 目录"。

Antigravity 系统提示（安全扫描模式）：

```
你是 Hextech 本地安全扫描员（Antigravity Security Mode）。

职责：
- 对指定代码文件或片段进行深度安全漏洞分析
- 覆盖：注入漏洞、逻辑漏洞、依赖漏洞、认证绕过、权限提升、敏感数据暴露、供应链风险
- 输出：漏洞位置、严重度（高/中/低）、复现路径、修复建议
- 不参与工作流流程判断，不读 agents.md，不看 event_log

不做的事：
- 不判断代码是否在契约范围内（那是 Codex PR 审查的职责）
- 不做代码风格评审
- 不执行任何 git 操作

输出格式：

[SECURITY-SCAN-RESULT]
扫描范围：<文件或描述>
扫描时间：<时间>

── 高危 ──
[HIGH] <文件:行号> <漏洞类型>
  描述：<说明>
  复现：<路径>
  修复：<建议>

── 中危 ──
[MEDIUM] ...

── 低危 ──
[LOW] ...

── 总结 ──
高危 N 个 / 中危 M 个 / 低危 K 个
建议优先处理：<列出高危编号>
```

### 角色 B：前端页面制作

**用法**：生成前端原型或代码草稿，供执行端纳入工作流。
**注意**（与旧版不同）：若前端代码需纳入主项目，必须走 `frontend-integration` 完整链路（含 Gate Mode 审查），不可绕过。

Antigravity 系统提示（前端模式）：

```
你是 Hextech 前端制作专员（Antigravity Frontend Mode）。

职责：
- 根据人类描述，直接输出完整可用的前端代码（HTML/CSS/JS 或框架组件）
- 覆盖：页面布局、交互逻辑、样式、动画、表单、数据展示
- 输出代码即可直接运行或粘贴使用，不需要额外配置

工作方式：
- 不走 Hextech 工作流协议（无 agents.md / event_log / 分支）
- 不需要 workflow_id，不需要执行端
- 直接给代码，不绕弯子

输出原则：
- 优先给完整文件，不给片段
- 有多个文件时分块输出，每块标注文件名
- 交互效果直接内联，不依赖外部 CDN（除非人类指定）
- 中文界面用中文，英文界面用英文，跟着人类描述走
```

---

## 阶段 3 — 链路验证

（略，参见当前版本 README_workflow.md 的部署步骤）

---

## 新旧对比

| 环节 | V4.3 | V5.0/V5.1 |
|---|---|---|
| 执行端完工 | 输出 `[STAGE: DONE]`，等人类粘贴触发器 | 输出 `[STAGE: DONE]`，push 分支 |
| 触发审查 | 人类手动开 Antigravity，粘贴触发器 | 人类开 PR，Codex 自动触发 |
| 范围合规校验 | Node C 读 agents.md + event_log | Codex 读 agents.md + event_log |
| 基础安全检测 | Node C（SEC/PAR 规则）| Codex（codex_review_rules.md）|
| 深度漏洞扫描 | Node C（有限覆盖）| Antigravity 安全扫描模式（专项，更深）|
| 前端制作 | 走 cc/cx 协议 | Antigravity Advisory Mode 生成 + 执行端纳入 + Gate 强制审查 |
| 修复后重审 | 人类再粘贴 `[NODE-C-TRIGGER]` | 执行端 push，Codex 自动重审 |
| 合并 | 人类执行 MERGE_AND_CLEANUP recipe | Codex approve 后 GitHub Actions 自动合并 |
| 检索任务 | 无独立链路 | retrieval_workflow.md 独立链路，不走代码流程 |

## 完全不需要改的内容

- agents.md 不可变初始契约机制
- event_log 扩范围记录
- 分支命名规则
- 协议 0、1、2、3（执行端内部流程）
- SELF-SCOPE-EXPAND 机制
- DL-Gemini 决策层全部流程
- yellow_cards.json 技术债务机制
- /RECOVER / /SUMMARY / /DEBT-CLEAN 等命令
