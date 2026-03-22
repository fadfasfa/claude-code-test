# Hextech Nexus - 英雄联盟极地大乱斗海克斯强化分析枢纽

![Hextech Nexus](https://via.placeholder.com/800x400/11111b/cdd6f4?text=Hextech+Nexus+极地枢纽)

**Hextech Nexus** 是一款专为《英雄联盟》极地大乱斗（ARAM）模式打造的深度海克斯强化系统分析工具。它通过实时 LCU 客户端嗅探、高频并发数据同步、以及基于统计学的贝叶斯胜率平滑算法，为玩家提供精确的英雄选择与海克斯搭配决策支持。

---

## 🌟 核心特性

### 1. 实时客户端联动 (LCU Integration)
- **智能嗅探**：自动检测英雄联盟客户端进程，获取选人阶段的待选英雄（Bench Champions）。
- **锁英雄热跳转**：当玩家锁定英雄时，Web 端会自动触发详情页跳转，秒速呈现当前英雄的最佳海克斯策略。
- **无缝通知**：基于 WebSocket 的全对等通信机制，确保客户端状态与浏览器界面毫秒级同步。

### 2. 深度数据引擎 (Analysis Engine)
- **贝叶斯胜率平滑**：应用贝叶斯推断对小样本数据进行加权平滑，消除低出场率英雄的胜率偏差。
- **Z-Score 综合评价**：
  - **英雄大盘**：结合贝叶斯胜率 (80%) 与出场率精度 (20%)，构建最权威的 T 级风云榜。
  - **海克斯推荐**：自适应综合评分算法（胜率增益 85% + 热度权重 15%），智能过滤“幽灵数据”。
- **热更新机制**：服务器启动时自动拉取最新全球大数据，并支持 CSV 文件 3 秒级的热重载监控。

### 3. 多级容错渲染系统 (Resilient UI)
- **智能 CDN 回退**：头像/图标加载链路：本地 Assets → 官方 DDragon CDN → CommunityDragon CDN。
- **Canvas 动态占位引擎**：当所有远程资源不可用时，利用 HSL 算法根据名称生成全球唯一的动态占位图。
- **资源自愈**：后台线程持续扫描并补全缺失的本地英雄资产。

---

## 📂 项目架构

```text
run/
├── web_server.py           # FastAPI 主服务器：提供 WebSocket、API 及静态资源服务
├── hextech_scraper.py      # 多线程高并发爬虫：多层 UA 池 + 指数退避重试 + 原子化存储
├── data_processor.py       # 核心算法工厂：负责 Z-Score、贝叶斯平滑及多级缓存管理
├── hextech_ui.py           # 桌面伴生悬浮窗：Tkinter 开发，支持穿透点击与 LCU 同步
├── hero_sync.py            # 资产管理引擎：DDragon 版本追踪与多源 CDN 同步逻辑
├── config/                 # 配置与持久化数据（JSON 映射表 & 离线数据）
├── assets/                 # 本地英雄头像资源池
└── static/                 # 现代化前端界面（HTML5 + TailwindCSS + JS）
```

---

## 🚀 快速启动

### 环境需求
- Python 3.8+
- 依赖安装：`pip install fastapi pandas requests psutil uvicorn pillow pywin32`

### 启动服务
```bash
python run/web_server.py
```
- 服务启动后，系统将自动在默认浏览器中打开控制台。
- 默认端口：`8000`（若被占用，脚本会自动寻找可用端口）。
- **后台就绪**：服务器启动后会在后台静默运行一次数据同步流程，确保数据时效性。

### 使用悬浮窗 (可选)
```bash
python run/hextech_ui.py
```
- 提供置顶悬浮窗，可紧贴英雄联盟客户端边缘，实时显示备战席英雄强度。

---

## 🛠️ 配置文件说明

- `Champion_Core_Data.json`: 英雄 ID、中英文名、称号映射。
- `Champion_Synergy.json`: 社区贡献的海克斯文章联动数据。
- `hero_aliases.json`: 英雄外号映射库，支持搜索拼音、简称、外号（如：快乐风男）。
- `Augment_Icon_Map.json`: 海克斯名称与 CommunityDragon 图片路径的精准映射。

---

## 🛡️ 开发约束 (The Iron Rules)

1. **算法不动性**：严禁修改 `data_processor.py` 中的 Z-Score 与贝叶斯权重计算公式，这是所有评级逻辑的基础。
2. **路径一致性**：所有本地资源必须通过 `hero_sync.py` 的路径变量访问，严禁硬编码绝对路径。
3. **资产防护**：海克斯图标请求必须经过 `unquote` 处理，确保对带空格/特殊字符名称的正确映射。
4. **内存安全**：Canvas 渲染后的 Buffer 必须显式释放，UI 循环中的 WebSocket 连接池需具备超时自愈能力。

---

## ⚡ 性能优化

- **O(1) 缓存**：计算结果基于 DataFrame 哈希进行持久化缓存，重复请求零计算开销。
- **异步池化**：LCU 轮询采用 asyncio 异步调度，减少对系统资源的占用。
- **图片预加载**：前端采用 `loading="lazy"` 结合 `Preconnect` 策略提升首屏加载速度。

---

## 🐞 故障排除

- **头像不显示**：可能是由于网络问题未成功拉取。程序会在启动后后台自动补全。
- **LCU 未检测到**：确保英雄联盟客户端已启动并且**不要以管理员权限运行**（防止权限隔离）。
- **海克斯图标缺失**：检查 `Augment_Icon_Map.json` 是否包含该海克斯名称的映射。

---

**Hextech Nexus - 让每一局 ARAM 都成为制胜局！**
re_Data.json
英雄核心数据映射表，格式如下：
```json
{
  "1": {
    "name": "黑暗之女",
    "title": "安妮",
    "en_name": "Annie"
  },
  "2": {
    "name": "狂暴之心",
    "title": "凯南",
    "en_name": "Kennen"
  }
}
```

### Champion_Synergy.json
英雄协同数据，包含社区贡献的联动文章：
```json
{
  "安妮": {
    "synergies": [
      "安妮|棱彩|SS|强力联动|15 | 2|作者：Hextech专家|原创|安妮配合【回归基本功】可以实现无限控场..."
    ]
  }
}
```

### Augment_Full_Map.json
海克斯阶级映射表：
```json
{
  "尖端发明家": "黄金",
  "回归基本功": "棱彩",
  "利刃华尔兹": "棱彩"
}
```

### hero_aliases.json
英雄别名映射表，支持多种称呼方式：
```json
{
  "德玛西亚皇子": ["hz", "huangzi", "皇子", "周杰伦"],
  "疾风剑豪": ["ys", "yasuo", "亚索", "快乐风男", "孤儿"]
}
```

## 🚀 使用指南

### 启动服务
```bash
# 启动Web服务器
python run/web_server.py

# 服务器将自动打开默认浏览器
# 默认端口: 8000 (如果被占用会自动寻找可用端口)
```

### 数据更新
- **英雄数据**：自动从DDragon同步，每小时检查一次
- **海克斯数据**：通过CSV文件更新，放置在config目录下
- **协同数据**：手动编辑Champion_Synergy.json文件

### 文件命名规范
- **英雄头像**：使用英雄ID作为文件名（如：1.png, 2.png）
- **海克斯图标**：使用海克斯名称URL编码作为文件名
- **CSV数据文件**：命名格式为 `Hextech_Data_*.csv`

## 🛡️ 开发约束

### 关键约束
1. **禁止修改**: `data_processor.py` 的 Z-Score 计算逻辑
2. **路径规范**: 必须使用 `hero_sync.py` 的 `ASSET_DIR` 变量
3. **内存管理**: Canvas必须显式销毁，防止内存泄漏
4. **API合规**: 原生Canvas API only，不允许第三方图形库
5. **闭包隔离**: 所有函数必须是纯函数，无外部依赖

### 三层协同架构
- **Node A (Claude Code)**: 高级策略执行，负责Canvas引擎等核心功能
- **Node B (Qwen)**: 轻量级执行，负责视觉布局和解析引擎
- **Node C (Roo Code)**: L3终审官，负责质量把控和合并审核

## ⚡ 性能优化

### 缓存机制
- **英雄数据TTL**: 1小时
- **CSV文件监控**: 3秒轮询检测变更
- **WebSocket连接池**: 复用连接，减少开销

### 资源优化
- **图片懒加载**: 海克斯图标使用loading="lazy"
- **CSS预连接**: 提前建立CDN连接
- **Tailwind JIT**: 按需生成CSS样式

## 🐞 故障排除

### 常见问题
1. **英雄头像不显示**：检查assets目录是否存在对应ID的PNG文件
2. **数据加载失败**：确认CSV文件格式正确，包含必要的列名
3. **LCU连接失败**：确保英雄联盟客户端已启动并处于选人界面
4. **海克斯图标缺失**：检查Augment_Icon_Map.json映射是否正确

### 日志位置
- **系统日志**: `run/config/hextech_system.log`
- **最大大小**: 1MB（自动轮转备份）

## 🤝 贡献指南

### 数据贡献
1. **英雄别名**：编辑`hero_aliases.json`添加新的外号映射
2. **协同文章**：按照指定格式添加到`Champion_Synergy.json`
3. **海克斯数据**：提供新的CSV数据文件

### 代码贡献
- 遵循现有的三层协同架构
- 保持函数纯度，避免副作用
- 添加适当的错误处理和日志记录
- 确保内存安全，特别是Canvas相关操作

## 📦 版本信息

- **当前版本**: V6
- **最后更新**: 2026-03-14
- **兼容版本**: League of Legends 14.3.1+
- **Python依赖**: FastAPI, pandas, requests, psutil, uvicorn, moviepy

---

*Hextech Nexus - 让每一局ARAM都成为制胜局！*