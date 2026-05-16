# docs 索引

本文件是 `docs/` 的短入口，给人和 agent 先判断应该读哪里。默认只读本索引和相关短规则，不自动读取 `docs/reference/` 或 `docs/archive/` 下的长文。

## 当前入口

| 路径 | 用途 | 默认是否读取 |
| :--- | :--- | :--- |
| `docs/workflows/` | 当前 workflow 规则、注册表、工具基线和 CC/CX 协作边界 | 是，按任务读取相关文件 |
| `docs/workflows/worktree-policy.md` | 唯一 worktree 策略 | worktree 相关任务读取 |
| `docs/workflows/work_area_registry.md` | 工作区和写入边界事实源 | 写入任务读取 |
| `docs/workflows/agent-skill-inventory.md` | 仓库级 Codex skill inventory | skill / agent 治理任务读取 |
| `docs/reference/` | 长文规则、learning 摘要和可选参考 | 默认不读，除非任务点名 |
| `docs/archive/` | 历史报告、旧方案和退休日志 | 默认不读，除非审计历史 |

## 上下文控制

- 入口文件只放索引和短规则。
- 历史报告进入 `docs/archive/reports/`。
- 旧错误输入进入 `docs/archive/learnings-retired/`。
- 仍有价值的 learning 摘要进入 `docs/reference/learnings/`。
- `run/` 不承载仓库级 workflow 规则或长期报告。
