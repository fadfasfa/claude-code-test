# Hextech 文档索引

r12 桌面独立窗口修复见 [desktop-stable28.md](desktop-stable28.md)：统一可信客户端选择、移除跨进程owner依赖及完整桌面冻结烟测。游戏内r11修复保留，真实位置/识别验收不由桌面测试代替。

当前r11深度修复见 [overlay-r11-deep-repair.md](overlay-r11-deep-repair.md)：条件前层、碎片否定、两阶段首反馈及显式有界取证；真机定位资格须单独确认。

当前 r10 联合回归修复见 [overlay-r10-regression.md](overlay-r10-regression.md)，桌面 `client_foreground` 与游戏内恢复/呈现验收按此增量及对应专项合同执行。

| 文档 | 用途 |
| :--- | :--- |
| [overlay-runtime.md](overlay-runtime.md) | Overlay 运行、诊断、契约、打包部署和真机验收的必读事实源 |
| [system-design.md](system-design.md) | 模块依赖、抓取门禁、进程和 generation 数据链路 |
| [overlay-recurrent-issues.md](overlay-recurrent-issues.md) | Overlay 历史故障、反模式与迭代前防重犯清单 |
| [overlay-display-repair-v3.md](overlay-display-repair-v3.md) | 游戏内显示增量、OCR 调度、评价摘要与未完成的真机/发布门 |
| [desktop-stable28.md](desktop-stable28.md) | 桌面右侧外贴、前台显隐、三秒调整宽限与混合 DPI 验收边界 |
| [overlay-capture-once.md](overlay-capture-once.md) | 显式单次游戏客户区诊断采集与缺失原帧的证据边界 |
| [data-layout.md](data-layout.md) | `resources`、`var/sources`、`var/snapshots` 的具体维护路径 |
| [overwolf-route.md](overwolf-route.md) | 暂停的未来路线，不参与当前实现 |

处理 Overlay、Sidecar、打包、部署或真机“无数据/延迟”问题时先读 `overlay-runtime.md`；反复修复、旧 Build 误测、全屏不可见、timeline 丢失或性能回归还必须对照 `overlay-recurrent-issues.md`。结构、路径、运行契约或发布权限变化时，代码、测试和以上当前事实源必须同步更新。
