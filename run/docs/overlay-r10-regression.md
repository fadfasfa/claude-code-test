# r10 桌面与游戏内真机回归修复

## 证据与范围

本轮针对 r9b `20260909T114026Z-51cd79d6a7cc`：两张用户截图显示桌面重复按钮/文字重叠/头像空块/背景缺画；9月9日 selection e4 的94个 active 观察约7.82秒均无READY，正常观察后按钮消失一次，随后81个button_hold没有新计算。截图缺画不能由根窗口visible或rect匹配反证。

保持Python/Tk、MSS、来源数据及身份准入规则；不新增运行服务、不切Overwolf、不改显示锚点/30和23px字号，不扩大统计/游戏数据采集权限。原始证据不改写；用户图片副本位于 `.artifacts/r10-evidence`，桌面变更前精确源码位于 `.artifacts/r10-desktop`。不存在可验证旧源码时，不把Git HEAD或9月8日安装包称作用户记忆中的稳定版。

## 桌面

默认 `client_foreground`：大厅、房间、选人只要客户端前台即允许完整外壳，不等待LCU或候选；失前台、最小化、实际游戏或用户关闭仍隐藏。保持同屏右贴、最低200逻辑像素及已有合法位置三秒调整宽限；不跳副屏、不自由拖动。具体生命周期以 `desktop-stable28.md` 为准。

窗口显隐/位置/布局/内容分别按变化驱动，稳定观察不能反复进入owner/WNDPROC装饰器；按当前句柄恢复层关系和region。头像生命周期与布局版本绑定。可见外壳与实际子控件映射单列；不透明原生fixture和真实用户像素不能用alpha=0的几何用例代替。

已在本机自有fixture定位两处根因：首次wrapper尚未建立时把WS_CHILD的Tk inner传入GWLP_HWNDPARENT，会变成reparent而非owner；之后GA_ROOT可指向外部owner。现在先建立隐藏wrapper并以wm frame核对本进程、TkTopLevel、非WS_CHILD；不通过不写窗口。另一处是layered surface重排残影：按钮实例唯一，关闭子树重绘会重新产生259个位于唯一刷新按钮bbox外的青色像素；真实布局变更时完整重绘自有wrapper/children后消失，稳定tick不强刷。

同条件失败图 `.artifacts/r10-desktop/after-screen/desktop-480-1.5-normal.png`；修后实际屏幕采集 `.artifacts/r10-desktop/final-avatars-v2/desktop-480-1.5-normal.png` 和 `desktop-260-1-narrow.png`，均为自有不透明面板，头像只读现有seed，未截图用户/League窗口。主线程已逐图检查重复按钮、开关重叠、背景、指标和头像。

## 游戏内

按钮坏帧撤销取证资格，恢复参考仅保留无身份几何，最长0.75秒；合法按钮消失→重现边允许下一帧一次MSS full-client重新检测，不能把button/residue当作scene confirmed。正常scene按既有连续确认要求恢复，旧OCR不回流；窗口/尺寸/DPI/局身份改变、暂停、结束及碎片清参考。没有原帧的e4只作控制流证据，不伪造名称真值。

Host相同语义请求不再每秒深复制/重新准备，独立快照复制和来源时效检查由现有后台线程完成；GUI只投递最新快照。来源identity忽略年龄计数，保留fresh/stale和来源身份变化；Stage preparing仍按既有两秒预算以100ms复核，不将等待永久缓存。相同等待文字/几何不重画并取消正在进行的映射；新内容仍保持版本失效。presentation兼容增加desired_visible、actual_visible、geometry/draw/callback版本。

性能工具按每个原始event_written取首次呈现作为独立样本；后续内容升级仍保留供完整三槽/重随验收，重复呈现数量单列。不能借去重删掉首次就慢的真实事件，也不能将内部composed当最终像素。

旧包归因通过r9b真实EXE内PYZ反汇编核实：同key仅在距上次请求小于1秒时跳过，满1秒就重提；request_version不变但prepared.host_read_at刷新，e4同revision连续约950/908/862/814ms、e6约990/874ms为该路径的真机证据。report.timing与presentation.bound_timing一致，不是计时字段串配。较早stable28/baseline/run与v3fix/baseline/run具有可读源码；冻结v15/r9仅按manifest/PYZ核对，不把当前HEAD当旧源码。

## 验证和发布边界

自动回归、原生不透明fixture、冻结包smoke、真实双屏与五局验收分别报告。桌面恢复≤300ms、隐藏≤100ms、跟随P95≤100ms；游戏首反馈≤300ms、捕获+识别P95≤180ms、完整正确三槽P95≤900ms、独立event→present P95≤100ms；错误READY、碎片误显、旧身份回流和无关槽破坏必须为0。

原生窗口测试串行。外部前台变化记录为受干扰/未完成，不改断言重跑到绿。没有真实画面时允许交付代码/自动检查，但不得宣称两屏缺画和游戏内消失已经修好；没有真机门时不替换正式安装或快捷方式。新候选独立命名，旧r9b、原图、历史日志和当前用户数据保留，不commit/push/清理。

## 已取得验证记录

- 最终非原生全量：1832 passed、19 subtests，33个native另行通过；`.artifacts/r10-tests/non-native-verified.xml`，149.87秒，退出码0。恢复两P2、头像模块行数和旧合同断言路径均已修正后复跑；此前失败记录保留。

- 非原生全量首轮1821 passed、19 subtests（后续独立审查新增反例另行最终复跑），`.artifacts/r10-tests/non-native-pass1.xml`。
- 串行原生全量33 passed、1825 deselected，`.artifacts/r10-tests/native-final.xml`。此前desktop扩展组92 passed/1 failed：全局foreground由explorer任务切换转到Chrome，窗口操作10.687ms；原始失败保留，不能用后次通过抹掉外部干扰记录。
- 修后不透明实际像素与只读seed头像用例通过；不代表两块实际League画面的验收结果。未取得真实五局/双屏十次新候选证据。

## 变更清单与独立复核

以下均相对run，仅列本轮实际增量，不将文件原有脏改归为本轮。

- 桌面：`src/hextech/interfaces/desktop/` 下的 `app.py`、`app_view.py`、`client_layer.py`、`window_presentation.py`、`responsive_view.py`、`runtime_window.py`、新 `avatar_loading.py`。
- 恢复：`src/hextech/infrastructure/vision/` 下的 `held_scene.py`、`runner_helpers.py`、`runner.py`、`sidecar_capture.py`、新 `scene_recovery.py`。
- Host：`src/hextech/interfaces/overlay/host_data_preparation.py`、`host_runner.py`、`host_presentation.py`。
- 验收：`tooling/acceptance/overlay_performance_probe.py`；`tests/test_desktop_window_presentation.py`、新 `test_desktop_r10_stability.py`、新 `test_desktop_avatar_loading.py`、新 `test_desktop_opaque_r10.py`、新 `test_overlay_scene_recovery.py`、新 `test_overlay_preparation_refresh_identity.py`、`test_overlay_first_feedback_order.py`、`test_overlay_performance_probe.py`、`test_dev_gate_runtime_desktop.py`（头像代码移动后的合同路径）。
- 文档：本文、`docs/README.md`、`docs/overlay-runtime.md`、`docs/desktop-stable28.md`。

新鲜上下文Astra high审查发现两个P2并已复核关闭：几何TTL过期不能消除连续场景确认门；恢复期间第二次按钮消失必须再次推进wall-clock OCR截止并清未READY观察，不延长原TTL。审查独立重跑原两序列，验证第一张normal仍拒绝、第二张才重签，旧1000.2票在1000.3截止后拒绝。GO仅针对源码/纯mock，不代表真机。

头像占位纯逻辑归入头像模块保持app_view≤800行，定向结构/布局/头像/稳定行为355 passed。Ruff通过，项目解释器Pyright 0 errors、1个既有`__all__`警告；没有新增忽略规则。一次额外Host审查请求因工具模型路由失败未产出有效审查，不能计作通过；最终采用上面的新鲜上下文独立审查。单任务额度未知，未进行额外付费跑分。

## 冻结候选

- `.artifacts/r10/releases/HextechCompanion-20260909-r10`，Build `20260909T135321Z-894762c1ca3d`。
- 源码指纹 `894762c1ca3d14b7ec6fd68d6d97202874b28a6e8ef278881e21d2728d4fea81`；280个源码清单文件与当前源码一致。
- 通过既有打包器离线构建，`--verified-snapshot-root` 使用只读9月8日现有snapshot；`--artifacts-dir .artifacts/r10 --release-name HextechCompanion-20260909-r10 --smoke-root .artifacts/s10`，未指定refresh-data/deploy。
- 构建退出码0；clean、stale_sidecar、populated_runtime三组全部ok，分别验证进程链、Build、MSS捕获排除及自有背景、模拟全屏阻止/无边框恢复和留存。原生DLL路径最大194/259。
- 正式安装EXE/manifest、`.previous` manifest及唯一桌面快捷方式SHA-256与本轮开始一致；快捷方式仍指向r9b，不视为部署r10。无正式Hextech进程由本轮启动，无Git staging/commit/push；仅复制指定证据、构建器移动本轮临时产物并回收自身临时目录，未删除旧包/用户资产。

### 用户追加授权后的快捷方式切换

用户随后明确要求“完成后替换快捷方式”。2026-09-09 22:06仅将原桌面 `Hextech伴生终端.lnk` 从r9b改指向本候选r10，复用既有部署模块的 `update_shortcut`，目标/工作目录/空参数/图标四字段回读匹配且EXE存在。旧入口原字节备份 `.artifacts/r10/shortcut-before-20260909T220631270.lnk` 经SHA-256校验一致。没有启动程序，没有覆盖 `C:/HextechCompanion` 或 `.previous`，没有清理旧包或Git操作；真机待验状态不因入口切换改变。
