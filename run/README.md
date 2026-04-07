# Hextech 伴生系统运行指南

主项目文档在当前目录：`PROJECT.md`。

## 项目概述

Hextech 伴生系统是一个本地运行的《英雄联盟》数据分析工具，提供英雄基础资料、海克斯推荐、桌面伴生界面和本地 Web 查询。

当前 `run/` 已收敛为源码侧调试/生成目录：

- `hextech_ui.py`、`web_server.py`：根目录兼容入口，实际逻辑位于 `app/ui/launcher.py` 和 `app/api/launcher.py`。
- `build.py`：唯一打包入口。
- `services/`：同步、抓取、预计算和运行时编排主层。
- `tools/`：仅保留被 `build.py` 调用的内部工具，不再作为独立 CLI 暴露。

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

```

### 打包

```powershell
python build.py
```

打包说明：

- 当前默认产物是 `PyInstaller --onedir`。
- 打包白名单会内置 `static/`、稳定 `config/` 资源和稳定 `assets/` 图片资源。
- 默认内置的稳定配置包括：`Champion_Core_Data.json`、`Champion_Alias_Index.json`、`Augment_Icon_Manifest.json` 及兼容图标映射文件。
- `Hextech_Data_*.csv`、`Champion_Hextech_Cache.json`、`Champion_List_Cache.json`、`Champion_Synergy.json`、运行日志和端口文件不会打包。
- 运行时读取顺序为：包内稳定资源 -> 本地运行目录覆盖资源 -> 在线刷新生成资源。
- 打包产物首次离线启动时，首页搜索、英雄头像和海克斯图标映射应可直接使用；高频战报数据只在源码调试链或后台刷新链中补齐。
- 打包产物只面向最终用户运行桌面程序，不暴露抓取、查询、清理或测试入口。

## 目录结构

```text
run/
├── app/                            # 入口与核心包
│   ├── core/                       # 共享别名 / 数据处理 / 运行时读取
│   ├── ui/                         # 桌面 launcher
│   └── api/                        # Web launcher
├── services/                       # 同步 / 抓取 / 预计算 / 运行时编排主层
│   ├── sync_hero_data.py           # 英雄基础资料与稳定资源落地
│   ├── scrape_hextech.py           # 海克斯抓取
│   ├── scrape_synergy.py           # 协同抓取
│   ├── scrape_augments.py          # 海克斯图标与目录补齐
│   ├── runtime_precomputed_cache.py# 预计算 API 缓存
│   ├── runtime_query.py            # 查询/归一逻辑
│   └── data_pipeline.py            # 抓取编排
├── tools/                          # 内部构建工具
├── build.py                        # 唯一打包入口
├── hextech_ui.py                   # 根目录桌面薄壳
├── web_server.py                   # 根目录 Web 薄壳
└── static/                         # Web 前端资源
```

## 常用接口

- `GET /api/champions`：英雄列表
- `GET /api/champion/{name}/hextechs`：英雄海克斯推荐
- `GET /api/champion_aliases`：首页搜索专用英雄别名索引
- `GET /api/augment_icon_map`：兼容保留的海克斯图标映射投影
- `GET /api/live_state`：当前 LCU 英雄选择状态
- `GET /api/synergies/{champ_id}`：英雄协同数据
- `POST /api/redirect`：浏览器跳转控制
- `GET /ws`：实时事件推送

## 运行说明

- `app/core/runtime_data.py` 是桌面、Web 和服务层共用的数据读取层。
- `backend_refresh.py` 统一负责编排英雄基础同步、海克斯刷新、协同刷新和图标补齐。
- `hextech_ui.py` 现在只是根目录兼容壳，实际桌面启动逻辑在 `app/ui/launcher.py`；`web_server.py` 也是同类兼容壳，实际 Web 启动逻辑在 `app/api/launcher.py`。
- `app/core/champion_aliases.py` 是唯一的首页别名标准源，查询归一逻辑已经下沉到 `services/runtime_query.py`。
- `app/core/data_processor.py` 负责把运行时数据整理成前端展示结构，供 Web 侧和薄壳入口复用。
- 打包时生成 `build/_bundle_runtime/bundle_manifest.json`，用于明确哪些稳定资源被白名单内置。
- 若本地目录已有更新版稳定资源，运行时会优先使用本地版本而不是包内副本。
- 本次重构已完成桌面壳 / Web 壳启动 smoke test，当前文档描述与代码路径以 `app/core` 和 launcher 为准。
