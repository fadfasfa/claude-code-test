## 任务状态机契约 (agents.md)

workflow_id：[wf-infra-v5-upgrade-20260307]
contract_version：[v1.0]
token_budget：max_cost_cny: 50 / max_retries: 5
分配节点：[Node A（Opus 档）]
路由决策依据：[本次任务涉及底层鉴权体系（verify_workspace.py）、DACL 防御纵深（Lock-Core.ps1）及静态扫描核心（post_check_diff.py）的重构，命中级别 A 敏感基建路径，强制升档以确保高风险基建操作的安全性与准确性。]
全局上下文状态：[意图已锁定待执行]
UAT_Status：[未开始]
Branch_Name：[ai-task-infra-v5-upgrade-20260307]
Target_Files：[Lock-Core.ps1, verify_workspace.py, post_check_diff.py]

---

### 前置条件 (Pre-conditions)
- HMAC 契约签名：[待核验]
- DACL 状态：[已解锁（破冰模式）]

### 后置条件 (Post-conditions)
- Lock-Core.ps1 具备目录级 Deny Delete/Rename 防御纵深及 audit_log.txt 单向追加配置。
- verify_workspace.py 具备计算并记录 agents.md 内容哈希的能力，支持 Node C 的双重锚定。
- post_check_diff.py 支持三点语法差异对比，并具备拦截 getattr/eval 注入的 AST 扫描能力。

### Verification_Command
pwsh -NoExit -File Lock-Core.ps1 -WhatIf

### Verification_Artifact
[.ai_workflow/test_result.xml]

### Verification_Result
[待填写]

### Thought_Log（事件序列，执行节点每 Step 完成后强制实时写入，禁止延迟批量回填）
- [Step 1 完成] 已创建任务分支 ai-task-infra-v5-upgrade-20260307，已在控制台输出回滚预案提示。
- [Step 2 完成] Lock-Core.ps1 已从 V1.0 升级至 V5.0。新增: (1) 目录级 Deny Delete + DeleteSubdirectoriesAndFiles 规则（同时阻止 Rename）; (2) audit_log.txt 仅追加模式（Deny WriteData/Delete + Allow AppendData）; (3) -WhatIf 模拟执行参数; (4) 四阶段防御纵深（MIC标签 -> 目录DACL -> 审计日志权限 -> 审计日志MIC）。
- [Step 3 完成] verify_workspace.py 已从 3 行 bypass 重构为完整 V5.0 鉴权引擎。功能: (1) HMAC-SHA256 契约签名校验; (2) agents.md 内容 SHA-256 + Git Hash 双重锚定; (3) 标准化换行符处理（CRLF->LF）确保跨平台哈希一致; (4) 哈希锚定写入 .ai_workflow/.contract_hash（JSON 格式）; (5) --anchor-only / --verify-only 命令行模式; (6) git hash-object 交叉校验。
- [Step 4 完成] post_check_diff.py 已从 V3.2 升级至 V5.0。变更: (1) get_changed_python_files 从两点语法 (main..branch) 改为三点语法 (main...branch)，基于 merge-base 正确提取增量; (2) 新增 check_sec001_injection() 函数，AST 遍历检测 eval()/exec()/getattr(obj, 动态值) 调用; (3) 批量模式与单文件模式均已集成 SEC-001 扫描。
- [Step 5 完成] 已在控制台输出破冰后置处理 SOP（git add -> git commit --no-verify -> Lock-Core.ps1 -> 状态推进至待审 -> 唤醒 Node C）。

---

### 确定性执行清单

- [ ] Step 1: 回滚预案确认
  - 目标文件：无物理修改
  - 结构化指令：请在控制台输出提示："[风险预告] 若本次基建升级导致后续环境异常，请执行 git reset --hard HEAD 放弃更改，并使用 .\Lock-Core.ps1 恢复旧版锁控。"
  - 风险提示：确保人类明确撤销路径。

- [ ] Step 2: 升级 DACL 锁控防线
  - 目标文件：Lock-Core.ps1
  - 结构化指令：修改 PowerShell 脚本逻辑。额外配置目录级（向 repo 根目录继承）的 Deny Delete 和 Deny Rename 规则；确保 .ai_workflow/ 和 .git/hooks/ 的高完整性锁继承；针对 audit_log.txt 配置文件权限为 Deny WriteData/Delete，但显式允许 AppendData。
  - 风险提示：PowerShell 的 Access Control List 操作极其敏感，注意权限类型的枚举映射是否正确，避免将目录完全锁死。

- [ ] Step 3: 重构鉴权与双重锚定引擎
  - 目标文件：verify_workspace.py
  - 结构化指令：在原有 HMAC 校验逻辑外，新增一个函数专门计算 agents.md 当前文件内容的 Hash（可使用 SHA256 或 Git 对应的 Hash 算法）。将该 Hash 值安全写入环境变量或受保护的本地缓存文件（如 .ai_workflow/.contract_hash），供 Node C 介入时提取并做双重锚定比对。
  - 风险提示：如果读取 agents.md 时未处理好编码或换行符（CRLF vs LF），会导致生成的 Hash 在不同节点计算时不一致，建议统一读取为二进制或标准化换行符。

- [ ] Step 4: 强化静态扫描引擎
  - 目标文件：post_check_diff.py
  - 结构化指令：修改 git diff 的调用逻辑，强制使用 `git diff main...<Branch_Name>`（三点语法）提取增量；引入 `ast` 模块，解析增量代码；增加规则拦截 AST 节点中的 `getattr` 或 `eval` 调用，若命中则打印高危警告 [SEC-001]。
  - 风险提示：仅扫描 Python 文件（.py）的 AST，遇到非 Python 文件的差异需妥善 bypass 或降级为正则扫描。

- [ ] Step 5: 破冰后置处理与重验提示
  - 目标文件：无物理修改
  - 结构化指令：由于是基建模式且无完全自动化验证，执行完成后，请在控制台输出提示："基建代码修改完毕。请人类执行以下操作：1. 确认无误后执行 `git add <三个目标文件>`；2. 执行 `git commit --no-verify -m "[INFRA-BYPASS] 升级 V5.0 防线与基建引擎 [wf-id: wf-infra-v5-upgrade-20260307]"`；3. 执行 `.\Lock-Core.ps1` 重新上锁；4. 将 agents.md 状态更新为 `[待审]`，再唤醒 Node C 终审。"
  - 风险提示：本步骤无自动化测试护航，强依赖人类按照 SOP 进行后续基建提交流程。

---

### Node C 终审记录（由 Node C 填写）
- 审查结论：[待审查]
- 当前修复重试次数：[0/5]
- 风险清单摘要：[待填写]
- 熔断原因（如有）：无
- UAT_Status 读取值：[未开始]

---

### 冲突避免与底层防线

- 语言规约：执行节点必须唯一使用简体中文（除代码块外）进行终端交互与日志输出。
- 权限隔离（通用）：执行节点禁止修改 DACL 保护范围内的文件（`.git/hooks/*`、`.ai_workflow/`、`Lock-Core.ps1`、`Unlock-Core.ps1`、`run_task.ps1`、`audit_log.txt`）。
- 权限隔离（agents.md 受控豁免）：执行节点对 agents.md 仅持有以下两种合法写入权：（a）协议 0 触发时的完整覆写；（b）协议 4 原子写入 `Verification_Result` 与 `全局上下文状态` 两字段。Node C 仅持有：正则替换 `全局上下文状态` 为指定终态、精准替换 `当前修复重试次数`。`UAT_Status` 字段由人类专属写入，任何节点均无权修改。
- 破冰规约：基建文件修改必须由人类启动破冰规约：执行 `Unlock-Core.ps1` → `git commit --no-verify -m "[INFRA-BYPASS] ..."` → `Lock-Core.ps1`。
- 范围控制：所有 `git add` 严禁全量，必须指定 `Target_Files` 中的目标文件。
- 分支隔离：所有代码修改必须在契约指定的任务分支（`Branch_Name`）内进行，禁止直接修改 main 分支。

### 节点间协议层引用
本契约执行期间，所有节点必须遵守 workflow_v5.0.md 中"节点间协议层"章节定义的写权限矩阵、阶段交接信号规范与合法状态转移规则。
