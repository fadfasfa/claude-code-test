# Hextech 伴生系统运行指南

主项目文档在当前目录：`PROJECT.md`。

## 项目概述

Hextech 伴生系统是一个本地运行的《英雄联盟》数据分析工具，提供英雄数据同步、海克斯推荐展示、桌面伴生界面和本地 Web 查询。

## 快速开始

### 安装依赖

```powershell
pip install -r requirements.txt
```

### 启动方式

```powershell
# 桌面伴生模式
python hextech_ui.py

# 仅启动 Web 服务
python web_server.py

# 后端数据整理模式
python hextech_query.py
```

### 打包

```powershell
python build.py
```

打包说明：

- 当前默认产物是 `PyInstaller --onedir` 的最小运行壳。
- 打包时只内置 `static/` 前端资源，不再预打包 `config/` 和 `assets/`。
- 首次启动会自动创建 `config/`、`assets/`，并在后台拉取英雄数据、海克斯 CSV、联动文章和图标缓存。
- `dist/_internal/` 是 PyInstaller 运行时依赖目录，不应手工删除其中单个文件。

### 清理缓存

```powershell
python cleanup.py
```

## 目录结构

```text
run/
├── alias_utils.py          # 英雄别名归一化与去重
├── apex_spider.py          # 协同数据抓取
├── backend_refresh.py      # 后台刷新调度
├── build.py                # 打包脚本
├── cleanup.py              # 清理脚本
├── data_processor.py       # 前端展示数据编排
├── champion_aliases.py     # 首页搜索专用别名索引读取与归一
├── hero_sync.py            # 英雄基础数据同步
├── hextech_query.py        # 后端数据整理入口
├── hextech_scraper.py      # 海克斯数据抓取
├── hextech_ui.py           # 桌面伴生界面
├── icon_resolver.py        # 图标映射与缓存
├── requirements.txt        # 依赖列表
└── static/                 # Web 前端资源
    ├── detail.html
    └── index.html
```

## 常用接口

- `GET /api/champions`：英雄列表
- `GET /api/champion/{name}/hextechs`：英雄海克斯推荐，返回 `comprehensive`、`winrate_only`、`top_10_overall` 及分阶级数组；单条海克斯对象包含 `tooltip` 与 `tooltip_plain`
- `GET /api/champion_aliases`：首页搜索专用的英雄别名索引，只供搜索联想和快捷检索使用
- `GET /api/augment_icon_map`：兼容保留的海克斯图标映射投影
- `GET /api/synergies/{champ_id}`：英雄协同数据
- `POST /api/redirect`：浏览器跳转控制
- `GET /ws`：实时事件推送

## 运行说明

- 首次启动会自动创建 `config/` 和 `assets/` 目录。
- 首次启动即使没有本地 `Hextech_Data_*.csv`，首页 `GET /api/champions` 也会先走远端轻量快照，避免页面长时间空白；完整海克斯数据会在后台继续生成。
- `hero_sync.py` 负责同步核心缓存，`web_server.py` 和 `hextech_ui.py` 都依赖这些本地文件。
- `hextech_ui.py` 会在启动后自动拉起 `web_server.py`，并通过 `config/web_server_port.txt` 读取实际端口，因此桌面模式和 Web 模式共用同一套本地服务。
- 图标与别名规则由 `alias_utils.py`、`icon_resolver.py` 统一处理，避免多处重复实现。
- `web_server.py` 启动后会自动检查海克斯统一目录与图标缓存；缺失项会在后台补齐，日志默认只保留任务级成功/失败摘要。
- `champion_aliases.py` 只负责读取和归一化 `config/Champion_Alias_Index.json`，不写回、不生成，且只服务首页搜索联想。
- `backend_refresh.py` 会清理死进程遗留的 `backend_refresh.lock`，避免异常退出后长时间卡住首轮刷新。
- `Augment_Icon_Manifest.json` 是运行时唯一海克斯目录，统一承载图标、阶级、tooltip 与纯文本 tooltip。
- 详情页悬浮窗只消费 `tooltip_plain`，并以单例 DOM 安全渲染纯文本描述，避免 HTML 注入风险。
- 右侧联动文章区和左侧榜单都优先消费详情接口返回的 `icon`，缺图时只保留真实失败状态，不再绘制占位图。

## 图像验证

建议在修复后至少抽检以下英雄详情页：

- 星界游神
- 异画师
- 诡术妖姬
- 狂战士
- 死亡颂唱者

验证标准：

- 每个海克斯条目都能渲染出 `<img>`。
- 图片无持续脚本占位回退，缺图时不会再绘制假图。
- 浏览器控制台没有 `Unexpected token`、占位图回退或资源错误处理相关脚本错误。
- 海克斯悬浮窗 hover 时能显示标题和纯文本描述，且不会插入脚本节点。

本次已验证的样本：

- 左侧海克斯图标链路：星界游神、异画师、诡术妖姬、狂战士、死亡颂唱者
- 右侧联动文章图标链路：双界灵兔（Aurora）详情页中的 `水豚空降`

## 清理说明

仓库中不保留预览页或临时截图脚本。若要清理本地运行缓存，使用 `cleanup.py`，不要手工删除 `config/` 中的核心缓存文件。
