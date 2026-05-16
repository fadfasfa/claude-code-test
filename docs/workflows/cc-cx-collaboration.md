# CC / CX Collaboration

本文件只保留协作定位；当前 CC -> CX 契约以 `docs/workflows/10-cc-cx-orchestration.md` 为准。

## Roles

- CC：planning、supervision、review。
- CX：读代码、写代码、跑命令并输出结构化结果。
- Codex 仍可独立工作；只有从 CC 发起调用时，CX 才作为 CC executor。

## Current Boundary

- CC 调用入口：`.\cx-exec.ps1`
- 真实 executor：`scripts/workflow/cx-exec.ps1`
- result root：`.state/workflow/tasks/<task_id>/`
- 默认 `CODEX_HOME`：`C:\Users\apple\.codex-exec`

## Safety

- 不打印完整 `CODEX_PROXY_API_KEY`。
- 不读取 `auth.json`、token、cookie 或 proxy secret。
- 不让仓库脚本绕过 wrapper 去调用 PATH 上的 `codex`。
