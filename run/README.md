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

# 终端查询模式
python hextech_query.py
```

### 打包

```powershell
python build.py
```

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
├── hero_sync.py            # 英雄基础数据同步
├── hextech_query.py        # 终端查询入口
├── hextech_scraper.py      # 海克斯数据抓取
├── hextech_ui.py           # 桌面伴生界面
├── icon_resolver.py        # 图标映射与缓存
├── requirements.txt        # 依赖列表
└── static/                 # Web 前端资源
    ├── canvas_fallback.js
    ├── detail.html
    └── index.html
```

## 常用接口

- `GET /api/champions`：英雄列表
- `GET /api/champion/{name}/hextechs`：英雄海克斯推荐
- `GET /api/champion_aliases`：英雄别名索引
- `GET /api/augment_icon_map`：海克斯图标映射
- `GET /api/synergies/{champ_id}`：英雄协同数据
- `POST /api/redirect`：浏览器跳转控制
- `GET /ws`：实时事件推送

## 运行说明

- 首次启动会自动创建 `config/` 和 `assets/` 目录。
- `hero_sync.py` 负责同步核心缓存，`web_server.py` 和 `hextech_ui.py` 都依赖这些本地文件。
- 图标与别名规则由 `alias_utils.py`、`icon_resolver.py` 统一处理，避免多处重复实现。
- `web_server.py` 启动后会自动检查海克斯图标缓存，缺失时会自动抓取并把结果写入 `config/augment_icon_audit.jsonl`。

## 清理说明

仓库中不保留预览页或临时截图脚本。若要清理本地运行缓存，使用 `cleanup.py`，不要手工删除 `config/` 中的核心缓存文件。
