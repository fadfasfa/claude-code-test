# Hextech 伴生系统运行指南

主项目文档位于当前目录的 [PROJECT.md](PROJECT.md)。本文只回答“这个工作区现在是什么、怎么启动、怎么打包、怎么验收”。

## 当前定位

`run/` 是 Hextech 伴生系统的实际运行工作区，包含桌面悬浮窗、本地 Web/API、数据处理、远端抓取、自愈修复和 PyInstaller 便携包构建。

当前项目目标：让打包后的便携目录在非仓库、空运行态目录中首次启动后 60 秒内可用；高频抓取数据不随包分发，由首次启动和 4 小时新鲜度策略触发后台刷新。

## 一眼看懂

| 维度 | 当前状态 |
| :--- | :--- |
| 主入口 | `python hextech_ui.py` 启动桌面伴生；`python web_server.py` 只启动 Web 服务 |
| 打包入口 | `python build.py`，不要另建平行打包流程 |
| 发布形态 | PyInstaller `--onedir` 未签名便携包 + `_portable.zip` |
| 启动硬门槛 | 打包产物空仓首启 60 秒内返回可用 Web/UI 热路径 |
| 高频数据策略 | `data/raw/` 和 `data/runtime/` 不进包；首次空仓必刷，之后超过 4 小时再刷 |
| 稳定资源策略 | 只把版本级稳定资源放进包，例如核心英雄数据、别名索引、稳定图标/manifest |
| 最近验收 | `python tools/smoke_packaged_startup.py --timeout 60`，严格空仓实测约 3.83 秒可用 |

## 快速命令

```powershell
# 安装依赖
pip install -r requirements.txt

# 桌面伴生模式
python hextech_ui.py

# 仅启动 Web 服务
python web_server.py

# 打包便携产物
python build.py

# 打包后空仓首启验收
python tools/smoke_packaged_startup.py --timeout 60
```

## 目录职责

```text
run/
├── build.py                    # 打包入口薄壳，委托 tools/build_bundle.py
├── hextech_ui.py               # 桌面入口薄壳，委托 display/hextech_ui.py
├── web_server.py               # Web 入口薄壳，委托 display/web_server.py
├── display/                    # 展示、桌面窗口、本地 Web/API、浏览器协同
├── processing/                 # 运行态路径、CSV/DataFrame、视图适配、后台编排
├── scraping/                   # 远端抓取、稳定资源同步、缺失产物自愈
├── tools/                      # 打包、清理、日志、自检、烟测工具
├── data/static/                # 版本级稳定数据文件
├── data/indexes/               # 版本级稳定索引文件
├── assets/                     # 稳定图片/图标资源入口
└── data/                       # 本地运行生成数据；不作为分发源数据
```

更细的文件职责、数据流和维护边界见 [PROJECT.md](PROJECT.md)。

## 运行态数据边界

### 可以随包分发

- `display/static/` 前端静态页面
- `data/static/` 中的版本级稳定数据文件
- `data/indexes/` 中的版本级稳定索引文件
- `assets/` 中的稳定图片/图标资源
- `Champion_Core_Data.json`
- `Champion_Alias_Index.json`
- `Augment_Icon_Manifest.json`
- 兼容图标映射文件

### 不应随包分发

- `data/raw/hextech/Hextech_Data_*.csv`
- `data/raw/synergy/Champion_Synergy.json`
- `data/runtime/state/*.json`
- `data/runtime/state/web_server_port.txt`
- `data/runtime/cache/`
- `data/runtime/locks/`
- `data/runtime/profile/`
- `data/runtime/logs/`
- 任何启动后生成、抓取、缓存、锁、日志或计算产物

### 首启会自动创建

- `data/raw/hextech/`
- `data/raw/synergy/`
- `data/runtime/state/`
- `data/runtime/cache/`
- `data/runtime/locks/`
- `data/runtime/profile/`
- `data/runtime/persisted/`
- `data/runtime/logs/`

## 打包与验收

`python build.py` 会生成：

- `dist/Hextech_伴生系统_YYYYMMDD/`
- `dist/Hextech_伴生系统_YYYYMMDD_portable.zip`
- 便携目录内的 `Hextech伴生终端.exe`
- 便携目录内的 `启动 Hextech.bat`
- 便携目录内的 `README_首次使用.txt`

发布前建议固定执行：

```powershell
python tools/smoke_packaged_startup.py --timeout 60
```

这个烟测会复制最新打包目录，删除复制品中的 `data/raw` 和 `data/runtime`，再启动 exe 检查：

- 运行态目录是否重新创建
- `web_server_port.txt` 是否新写入
- `startup_status.json` 是否新写入
- `_internal/data/runtime` 是否不存在
- `/`、`/api/startup_status`、`/api/champions`、`/detail.html?champion=1`、`/api/synergies/1` 是否可访问

## 常用接口

- `GET /api/champions`：英雄列表
- `GET /api/champion/{name}/hextechs`：英雄海克斯推荐
- `GET /api/champion_aliases`：首页搜索专用英雄别名索引
- `GET /api/augment_icon_map`：海克斯图标映射
- `GET /api/live_state`：当前 LCU 英雄选择状态
- `GET /api/synergies/{champ_id}`：英雄协同数据
- `POST /api/redirect`：浏览器跳转控制
- `GET /ws`：实时事件推送

## 维护入口

- Web 路由优先改 `display/web_api.py`
- Web 生命周期、端口、浏览器、LCU、缓存逻辑优先改 `display/web_runtime.py`
- 桌面后台线程、轮询、跳转、资源加载逻辑优先改 `display/ui_runtime.py`
- 桌面控件结构优先改 `display/hextech_ui.py`
- 纯数据转换、DataFrame 清洗、视图适配优先改 `processing/`
- 远端抓取、稳定资源同步、自愈修复优先改 `scraping/`
- 打包链路变更时同步检查 `tools/build_bundle.py`、`tools/bundle_manifest.py`、`tools/runtime_bundle.py` 和本文档
