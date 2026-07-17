# Hextech 模块化单体

`run/` 是 Desktop、Web、Overlay、DataService 与三个抓取来源共享的运行工作区。源码采用 `src` layout；可提交资源只在 `resources/`；所有本机可写状态只在 `var/`。不存在旧目录 fallback 或根脚本入口。

## 入口

安装为 editable package 后使用 `pyproject.toml` 的命令：

```powershell
python -m pip install -e .
hextech-desktop
hextech-web
hextech-overlay
hextech-data-service
hextech-supervisor
```

开发工具直接以模块运行：

```powershell
python -m tooling.checks.dev
python -m tooling.build --help
python -m tooling.acceptance.smoke_packaged_startup --help
```

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
│   ├── runtime/            # supervisor、settings、进程环境
│   └── bootstrap/          # 可执行 composition roots
├── resources/
│   ├── catalog/            # 英雄、海克斯、版本目录
│   ├── assets/             # 稳定图片与 Vision 资源
│   ├── seeds/              # 首启完整 generation
│   └── evidence/mayhem/    # 可提交来源证据
├── var/                    # sources、snapshots、state、ipc、cache、logs、reports、locks
├── tests/
├── tooling/
├── docs/
└── pyproject.toml
```

依赖方向固定为 `contracts <- modules <- interfaces/infrastructure <- runtime <- bootstrap`。只有 DataService 能发布 generation；抓取器只能发布各自完整 source run。

详细数据路径和 current 指针规则见 [docs/data-layout.md](docs/data-layout.md)，进程与数据链路见 [docs/system-design.md](docs/system-design.md)。
