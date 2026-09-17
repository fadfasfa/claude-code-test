# Hextech 文档索引

当前源码候选（2026-09-16）：DataService 使用 `IncrementalRefreshService`，snapshot v3 支持排行先到/完整英雄独立发布，Host 异步输入并在首次选择截止冻结统计代。ARAMKit/Apex/Mayhem 每4小时检测，只有版本变化或本地缺损才更新。当前合同见 [version-driven-refresh.md](version-driven-refresh.md)、[overlay-runtime.md](overlay-runtime.md) 与 [system-design.md](system-design.md)。这不是正式安装或全真机通过声明。

历史 r11 深度修复见 [overlay-r11-deep-repair.md](overlay-r11-deep-repair.md)：条件前层、碎片否定、两阶段首反馈及显式有界取证；真机定位资格须单独确认。

历史 r10 联合回归修复见 [overlay-r10-regression.md](overlay-r10-regression.md)，桌面 `client_foreground` 与游戏内恢复/呈现验收按此增量及对应专项合同执行。

| 文档 | 用途 |
| :--- | :--- |
| [version-driven-refresh.md](version-driven-refresh.md) | 四小时检测、条件请求、版本/完整性绑定与失败退避；检测不等于全量更新 |
| [overlay-adaptive-recognition.md](overlay-adaptive-recognition.md) | 独立识别Catalog、逐身份能力、严格OCR与有界选择证据；人工回放及剩余真机验收 |
| [overlay-case-baseline.md](overlay-case-baseline.md) | 原始问题、人工真值回归与缺失原帧边界 |
| [overlay-refresh-v2-history.md](overlay-refresh-v2-history.md) | 已退役 V2 刷新/锁代/年龄策略的冻结历史，不是生产入口 |
| [overlay-runtime.md](overlay-runtime.md) | Overlay 运行、诊断、契约、打包部署和真机验收的必读事实源 |
| [system-design.md](system-design.md) | 模块依赖、抓取门禁、进程和 generation 数据链路 |
| [overlay-recurrent-issues.md](overlay-recurrent-issues.md) | Overlay 历史故障、反模式与迭代前防重犯清单 |
| [overlay-display-repair-v3.md](overlay-display-repair-v3.md) | 游戏内显示增量、OCR 调度、评价摘要与未完成的真机/发布门 |
| [desktop-stable28.md](desktop-stable28.md) | 桌面右侧外贴、前台显隐、三秒调整宽限与混合 DPI 验收边界 |
| [overlay-capture-once.md](overlay-capture-once.md) | 显式单次游戏客户区诊断采集与缺失原帧的证据边界 |
| [data-layout.md](data-layout.md) | `resources`、`var/sources`、`var/snapshots` 的具体维护路径 |
| [overwolf-route.md](overwolf-route.md) | 暂停的未来路线，不参与当前实现 |

处理 Overlay、Sidecar、打包、部署或真机“无数据/延迟”问题时先读 `overlay-runtime.md`；反复修复、旧 Build 误测、全屏不可见、timeline 丢失或性能回归还必须对照 `overlay-recurrent-issues.md`。结构、路径、运行契约或发布权限变化时，代码、测试和以上当前事实源必须同步更新。

桌面r12独立窗口与物理显示边界继续以 [desktop-stable28.md](desktop-stable28.md) 为准。旧六文件仅在 tests/support/legacy_refresh 保留回归，生产不能导入；原始响应预算拒绝不会自动清理，v3 receipt schema2已绑定完整单元闭包和控制状态。30秒性能、fresh网络全链与原生五局验证保持未完成。
