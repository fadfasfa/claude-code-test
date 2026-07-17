# Hextech 稳定资源

`resources/` 只存放随源码或安装包分发的不可变输入。运行中的抓取结果、current
指针、generation、缓存、状态、日志和报告全部写入 `var/`。

| 路径 | 内容 | 写入方 | 消费方 |
| :--- | :--- | :--- | :--- |
| `catalog/` | 英雄、海克斯和版本目录 | catalog 维护工具 | parser、DataService、界面资源 API |
| `assets/` | 稳定图片和 Vision 资源 | 资源维护工具 | Web、Overlay、Vision |
| `seeds/` | 已验证的完整 generation | 构建维护流程 | 首次启动播种器 |
| `evidence/mayhem/` | 可审计 Mayhem 来源证据 | 维护流程 | parser、离线审计 |

界面不直接拼装这些输入。Desktop、Web 和 Overlay 只通过
`DataSnapshotView` 读取 `var/snapshots/current.v1.json` 固定的完整 generation。

维护时同步检查 `manifest.v1.json`、构建白名单和资源测试。禁止把凭据、cookie、
浏览器 profile、本机缓存或失败 run 当作稳定资源提交。
