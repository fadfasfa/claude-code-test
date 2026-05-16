# CC / CX Collaboration

## 定位

- Claude Code / CC 是大脑、监工和最终验收者：负责拆解任务、设定边界、审查结果和决定是否接受。
- Codex / cx 是手和眼睛：负责读代码、写代码、跑命令，并把结果写成结构化产物。
- Codex 仍可以独立工作。只有从 CC 发起调用时，`cx` 才作为 CC 的执行器。

## 两种模式

### Codex 独立模式

用户直接给 Codex 下任务时，Codex 按 `AGENTS.md`、`PROJECT.md`、`docs/workflows/work_area_registry.md` 和任务上下文工作，不需要经过 `cx-exec.ps1`。

### CC 调用模式

CC 调用时使用仓库根入口：

```powershell
.\cx-exec.ps1 -TaskId "<task-id>" -TaskDescription "<task>" -Profile implement
```

调用链路：

1. `cx-exec.ps1`
2. `scripts/workflow/cx-exec.ps1`
3. `C:\Users\apple\codex-maintenance\codex-exec-wrapper.exe`
4. `C:\Users\apple\AppData\Local\OpenAI\Codex\bin\codex.exe`

根目录入口只做参数兼容和转发；真实执行逻辑在 `scripts/workflow/cx-exec.ps1`。

## Wrapper 与 PowerShell 的职责

- C# wrapper 给 VS Code Codex 用，同时也作为 CC 调用链路里的真实 Codex 启动器。
- PowerShell `cx-exec.ps1` 给 Claude Code 调用，用来检查环境、组织任务目录、调用 wrapper，并生成结构化结果。
- 两者都保留；不要把 VS Code wrapper 改成仓库脚本，也不要让仓库脚本绕过 wrapper 去调用 PATH 上的 `codex`。

## 结果位置

每个 CC 调用任务写入：

```text
.state/workflow/tasks/<task_id>/result.json
.state/workflow/tasks/<task_id>/codex.log
.state/workflow/tasks/<task_id>/codex.err.log
```

根目录不再作为临时产物区，不再写新的 `CODEX_RESULT.md`。

## 安全边界

- 不打印完整 `CODEX_PROXY_API_KEY`。
- 不读取 `auth.json`、token、cookie 或 proxy secret。
- 默认 `CODEX_HOME` 是 `C:\Users\apple\.codex-exec`。
- proxy 是否可用只能由 health、配置和实际 smoke 结果共同判断。

