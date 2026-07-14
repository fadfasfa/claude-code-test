# Hextech `run/` 项目入口

`run/` 是 Hextech 伴生系统业务工作区，包含桌面客户端、本地 Web/API、数据处理、Python/Tk/Vision Overlay、刷新自愈和便携包构建。

## 先读

- 当前系统架构、路径、启动预算、数据链路、运行态和验收：[docs/system-design.md](docs/system-design.md)
- 暂停的 Overwolf/GEP 未来路线：[docs/overwolf-route.md](docs/overwolf-route.md)
- 运行区文档索引：[docs/README.md](docs/README.md)
- 数据分类事实源：`data/data_manifest.v1.json` 与 `data/DATA_USAGE.md`

## 关键入口

| 任务 | 入口 |
| :--- | :--- |
| 桌面启动 | `hextech_ui.py` -> `hextech/display/desktop/app.py` |
| Web 启动 | `web_server.py` -> `hextech/display/web/app.py` |
| Runtime Supervisor | `hextech/runtime_supervisor.py` |
| Overlay 生命周期 | `hextech/overlay/lifecycle.py` |
| Vision | `hextech/overlay/vision/` |
| 后台刷新 | `hextech/core/refresh.py` |
| 打包 | `.\.venv\Scripts\python.exe build.py` |
| 自动化 | `.\.venv\Scripts\python.exe -m pytest -q` |
| Overlay gate | `.\.venv\Scripts\python.exe tools/dev_checks.py --overlay-only --deep` |

## 稳定边界

- 源码态只使用 `run/.venv` 的 Python 3.11；打包也从该环境运行。
- 运行态统一写 `data/runtime/`；冻结态写 `%LOCALAPPDATA%/HextechNexus/data/runtime/`，不写便携包目录。
- 网络抓取不阻塞桌面首屏、Web 本地可用或 Overlay fallback。
- `data/runtime/**`、日志、cache、profile、lock、debug 和抓取 raw 不进入发布资源。
- 当前正式 Overlay 是 Python/Tk/Vision；Overwolf 不在生产链路。
- 改路径、启动预算或进程所有权时，必须同步代码、测试和 `docs/system-design.md`。
