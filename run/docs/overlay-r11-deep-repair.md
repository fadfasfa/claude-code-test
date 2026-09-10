# r11 前层、场景否定与首反馈深度修复

## 证据与实现边界

基线为r10 `20260909T135321Z-894762c1ca3d`。桌面原生实验已复现：后台普通窗NOACTIVATE抬升和后台TOPMOST窗都能遮住面板，原binder仍判正常。游戏e8时间线三槽OCR为魔法抗性碎片/技能急速碎片/迅捷碎片（0.996以上），模板右槽却确认1088终极刷新；这是内部冲突证据，不代替人工图像真值。

桌面保留r10 wrapper安全、子树重绘、头像版本与同屏右贴；改为仅前台资格存在期间的TOPMOST租约。稳定布局也执行独立有界层检查：100ms一次、24个前序窗口/2ms软预算，不读其他应用标题内容；每滚动秒最多3次纠正。失前台、游戏、隐藏、退出不受配额限制，立即撤销自身置顶并隐藏。客户端/blocker样式和位置不修改；不能以probe未完成宣称无遮挡。

场景否定独立于production pool：完整已核对名称exact规范化且confidence>=0.95，与session/epoch/slot generation/frame/input SHA/fingerprint/time/cutoff绑定。一个可信槽只暂停身份/数值，两个不同槽进入既有body_shard latch。否定必须先于正面归约，但空场景仍推进既有退出宽限；不能让六秒否定证据寿命吞掉场景结束。

同一个Tracker的begin_frame/finish_frame拆开场景与身份。场景轻量检查完成后先发布必要反馈，再执行模板投影；OCR复用名称裁剪提前提交，异步证据仍需绑定/冲突仲裁。同帧只推进一次scene、登记一次槽观察；重随在早期阶段只推进目标槽一次代际，不能先发布已失效的旧READY。暂停/reset/新帧使旧票据失效。

Host保持相同内容不重画、同key不重提、后台时效检查；预热成功且无需可见结果标为prewarmed，不再被当成失败反复准备。

## 显式有界诊断与位置校准

默认不保存图片。使用固定位置的一次性请求开启诊断，30秒请求有效期并绑定当前Build；不重启服务、不改配置/快捷方式。一次会话最多3张有效完整客户区定位帧、12组三槽名称/图标ROI、总64MiB；复用现有低优先级writer，失败/队列满/预算耗尽必须记录不完整，不能污染生产识别结论。截图与原始frame及窗口/显示器/DPI绑定，candidate/冲突/结束也能记录，不以READY作为采集门。

位置只按同规格真实原图校准；当前两屏2560×1440和2560×1600分别确认，1920规格另行验证。保留30/23px及统计/联动信息，不靠缩字、改OCR ROI或拉伸图片修显示。没有真实同规格完整画面时不改锚点、不升级pending_real_device，必须明确报告未完成；禁止把实验窗口画面当League定位真值。

## 验收

桌面前台恢复<=300ms、失前台降层隐藏<=100ms；游戏反馈<=300ms、捕获+识别P95<=180ms、完整正确三槽P95<=900ms、独立event→present P95<=100ms；错READY/碎片误显/旧身份回流/无关槽破坏为0。分母来自人工标注选择span，不用自身active/body_shard分类排除失败。重复帧/心跳不增加成功数。

单元与全量、串行原生、冻结包smoke、两屏实际游戏五局分别记录。保持原始图像/日志、旧候选、正式安装、快捷方式和既有脏改，无commit/push/清理/部署。打包通过不是四条真机链路的完成声明。

## 实际验证与未完成项（2026-09-10）

- 非原生全量：`python -m pytest -q -k 'not native' --junitxml=.artifacts/r11-tests/non-native-verified.xml`，1870 passed、19 subtests，135.17秒；之后补e8同帧负证据优先回归，two_phase+negative定向18 passed。
- 串行原生：`python -m pytest -q -k native --junitxml=.artifacts/r11-tests/native-final.xml`，35 passed。桌面子任务原生遮挡/撤销测试没有修改owner样式或前台；撤销+隐藏分别约0.60/0.67ms，仅是自建窗口测量，不代表League实际结果。
- Ruff通过；项目venv解释器Pyright 0 errors、1个既有`__all__` warning；diff whitespace通过。首轮并发worker测试依赖30ms sleep曾观测4而非6并发，生产抓取代码未改；测试改为有界第一批同步并保持6worker断言，单测及全量通过。
- 独立审查发现并复核关闭5项：单槽negative吞掉空场景退出；重随早反馈带旧READY；同Build重启重放诊断请求；negative最终事件丢raw槽证据；observations目录reparse遗漏。全部有对应新反例，不降低身份阈值或跳过测试。
- r10 e8时间线原样保存在 `.artifacts/r11-negative`；桌面原源码/验证摘要在 `.artifacts/r11-desktop`。并行negative/diagnostic任务后续停在待初始化，主线程接回收口，没有将未运行工作算作已验证。
- 检查时无正在运行的League游戏进程；现有display-20260906中的完整图主要是7月fixture，报告也标candidate_not_deployed。没有当前同规格完整游戏定位帧及人工内框真值，因此 **2560×1440、2560×1600及1920两规格位置校准均未完成**，显示锚点与30/23px不改，不升级pending_real_device。
- 碎片完整名称表目前包含六个由旧ROI或r10绑定日志核对的名称；新场景/新名称需继续补独立证据，不能据此声称所有碎片或总体识别率已达标。

## 最终候选与诊断入口

- 独立候选 `.artifacts/r11/releases/HextechCompanion-20260910-r11`，Build `20260910T024722Z-5da6985aca8b`。
- 源码指纹 `5da6985aca8b60b13c3f4c0ab1795762e9c5d9024305b1394463cc05ae7973ad`，286个源码清单文件与当前源码一致。
- 既有打包器使用只读snapshot `20260909T144950-78acf731dd`，不刷新远端数据；参数为 `--verified-snapshot-root <真实snapshots> --artifacts-dir .artifacts/r11 --release-name HextechCompanion-20260910-r11 --smoke-root .artifacts/s11`，未传deploy。clean/stale_sidecar/populated_runtime三组全部ok；最大原生DLL路径194/259，构建退出码0。
- 显式诊断命令：在run中执行 `.venv/Scripts/python.exe -m tooling.diagnostics.overlay_capture_session --request`。必须已有支持此功能且心跳新鲜的Sidecar，旧版本明确拒绝；请求绑定Build和sidecar_instance，30秒内消费，重启不可重放。同一实例去重，已有采集期间不重置额度；等待下一次选择最多120秒，结束或限额后不自动重开。
- 请求只写固定 `var/state/diagnostic_capture_request.v1.json`；不启动/重启程序、不修改长期设置。候选运行后用户明确请求才启用；本轮没有执行该请求或真实游戏截图。输出仅在 `var/debug/overlay_vision/explicit_capture_sessions`，父链拒绝reparse；session名不接受原始路径片段，失败/丢弃/预算终态保留“不完整”。
- 当前快捷方式仍指向r10。正式EXE、`.previous`和旧候选未改；无Git staging/commit/push，无删除旧资产。仅构建器整理本轮产物并回收自身临时目录，不将其描述为完全零文件移动。

## 本轮修改文件

以下相对run，仅列本轮增量，文件内既有脏改仍归用户：

- Desktop：`src/hextech/interfaces/desktop/foreground_layer.py`（新增）、`client_layer.py`、`app_view.py`、`window_presentation.py`。
- Vision：`src/hextech/infrastructure/vision/frame_admission.py`、`frame_pipeline.py`、`scene_negative.py`、`diagnostic_capture_session.py`、`diagnostic_capture_control.py`（新增）；`state.py`、`sidecar_detection.py`、`ocr_completed.py`、`ocr_shadow.py`、`completed_evidence.py`、`slot_frame.py`、`runner.py`、`runner_helpers.py`、`runner_lifecycle.py`、`roi_diagnostic_writer.py`、`capture_geometry.py`、`sidecar_diagnostics.py`。
- Host：`src/hextech/interfaces/overlay/host_data_preparation.py`。
- 工具：`tooling/diagnostics/overlay_capture_session.py`（新增）。
- 测试：新增 `test_desktop_foreground_layer.py`、`test_overlay_scene_negative.py`、`test_overlay_two_phase_frame.py`、`test_overlay_diagnostic_capture_session.py`、`test_overlay_diagnostic_capture_control.py`；更新 `test_desktop_client_layer.py`、`test_desktop_window_presentation.py`、`test_desktop_responsive_native.py`、`test_overlay_data_preparation.py`、`test_aramkit_source.py`（仅确定性测试同步）。
- 文档：本文、`docs/README.md`、`docs/desktop-stable28.md`、`docs/overlay-runtime.md`。
