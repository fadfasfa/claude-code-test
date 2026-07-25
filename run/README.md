# Hextech 模块化单体

`run/` 是 Desktop、Web、Overlay、DataService 与三个抓取来源共享的运行工作区。源码采用 `src` layout；可提交资源只在 `resources/`；所有本机可写状态只在 `var/`。不存在旧目录 fallback 或旧根入口兼容。

## 入口

运行时依赖以 `pyproject.toml` 为唯一事实源。安装为 editable package 后使用：

```powershell
python -m pip install -e .
hextech-desktop
hextech-web
hextech-overlay
hextech-data-service
hextech-supervisor
```

开发门禁工具可额外安装：

```powershell
python -m pip install -e .[dev]
```

开发工具直接以模块运行：

```powershell
python -m tooling.checks.dev
python -m tooling.build --help
python -m tooling.acceptance.smoke_packaged_startup --help
```

Windows 下两个最高频入口直接位于 `run/` 根目录：

```powershell
.\启动Hextech.ps1
.\调试Web前端.ps1
.\调试Web前端.ps1 -ProbeOnly
.\调试Web前端.ps1 -WithOverlay
```

较低频的 Overlay 和整机检查保留在 `tooling/dev/`：

```powershell
.\tooling\dev\start_overlay_probe.ps1
.\tooling\dev\verify_machine.ps1
.\tooling\dev\verify_machine.ps1 -RequireRunningWeb -RequireConsistentGeneration
```

脚本优先使用 `run/.venv` 中的 editable 命令，并输出当前 generation、状态文件、Web 端口文件和日志目录。
`调试Web前端.ps1` 默认只启动 DataService 与 Web，不创建 Desktop、客户端悬浮窗或 Overlay；`-WithOverlay` 才额外启动 Overlay host 与 Vision sidecar。
Web 实际端口写入 `var/state/web_server_port.txt`；`-ProbeOnly` 会等待 DataService 和 Web 就绪、验证首页 HTTP 200 后回收全部测试进程。
完整系统运行时可使用两个 `Require*` 开关，把 Web 监听和 Desktop/Web/Overlay 共用 generation 提升为硬检查。

Vision 人工探针：

```powershell
.venv\Scripts\python.exe -m hextech.infrastructure.vision.sidecar --once --preset auto --write-event
.venv\Scripts\python.exe -m hextech.infrastructure.vision.sidecar --loop --preset auto --write-event
```

## 目录

```text
run/
├── src/hextech/
│   ├── contracts/          # ID、DTO、FailureKind、SourceHealth
│   ├── modules/            # data、game_context、vision、recommendation、session
│   ├── interfaces/         # desktop、web、overlay
│   ├── infrastructure/     # scraping、persistence、lcu、vision、observability
│   └── bootstrap/          # 可执行 composition roots
├── resources/
│   ├── catalog/            # 英雄、海克斯、版本目录
│   ├── assets/             # 稳定图片与 Vision 资源
│   ├── seeds/              # 首启完整 generation
│   └── evidence/           # 可提交离线来源证据
├── var/                    # catalog、sources、snapshots、state、user-data、cache、logs、reports、locks
├── tests/
├── tooling/
├── docs/
├── 启动Hextech.ps1
├── 调试Web前端.ps1
└── pyproject.toml
```

依赖方向固定为 `contracts <- modules <- interfaces/infrastructure <- bootstrap`。具体实现组装只允许出现在 `bootstrap`。只有 DataService 能发布 generation；抓取器只能发布各自完整 source run。`resources/**` 运行时只读，在线 Catalog 和图片只写入 `var/**`。

详细数据路径和 current 指针规则见 [docs/data-layout.md](docs/data-layout.md)，进程与数据链路见 [docs/system-design.md](docs/system-design.md)。Overlay、Sidecar、打包部署或真机诊断必须先读 [docs/overlay-runtime.md](docs/overlay-runtime.md)，并在测试前核对安装包与运行态 `build_id` 一致。
