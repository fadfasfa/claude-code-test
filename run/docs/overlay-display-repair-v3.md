# 游戏内显示修复 v3 的实现与验收边界

## 2026-09-09 链路修复（未部署、真机待验）

- 生产捕获固定 `mss==10.2.0`；一个 Sidecar 线程复用一个实例并在退出时关闭。不抓整桌面/多屏后裁剪，不新增运行服务。联合 ROI 由实际消费者及允许的 transform 包络派生并缓存；无法确定范围时只捕获完整游戏客户区，失败不偷偷切后端。
- `capture_regions_valid` 区分真实像素和补边，检测前核对完整场景包络，识别前核对名称/图标 ROI；不把 `frame.size` 当作全图已捕获。生产捕获后再次核对前台、HWND绑定的客户区、游戏实例和窗口模式；漂移帧暂停且不计成功观察。
- `HeldSceneEvidence` 只来自正常 scene gate 已确认的当前 epoch；保留态必须仍与当前游戏/窗口/几何绑定且按钮可见，没有 modal 或碎片。使用已确认 transform，仅继续未 READY、未遮挡且名称有效槽的模板/OCR取证，保持0/1/2索引；不改 scene_present、不自行开 epoch、不放宽 exact/0.95/独立3-of-5。
- 普通失败帧、结束、暂停和身份/几何变化废弃取证租约；槽位代际及已有回流仲裁继续拒绝旧证据。`evidence_starved`展示“识别未确认”但保持detecting并允许恢复，不冒充无统计或识别成功。
- Host先映射轻量shell，再请求后台准备；保持16ms选择态和50ms局中等待态，逐槽last-good不清空。几何或绘制版本改变会作废旧映射/复核/重试回调；同尺寸平移不隐藏，DPI/尺寸变化撤下旧布局。
- 打包呈现自检必须使用生产同源MSS捕获排除探针，`mss_capture_exclusion_v1`不能由旧Pillow探针代验；自有fixture成功仍不是League最终画面证据。
- 本轮不改显示锚点和30/23px字号。四规格真实内框、人工真值、五局、双屏10次与性能门仍须单列；没有对应证据不提升pending_real_device资格，不部署、不修改正式快捷方式、不提交推送。

### 本轮实际变更清单

以下路径均相对 `run/`，仅列本轮增量；不表示这些文件原有脏改由本轮产生。

- 依赖：`pyproject.toml`（仅在本工作区 `.venv` 安装 MSS 10.2.0）。
- 共享捕获：`src/hextech/modules/vision/screen_capture.py`。
- Vision：`src/hextech/infrastructure/vision/` 下的 `capture_geometry.py`、`held_scene.py`、`sidecar_capture.py`、`sidecar_detection.py`、`sidecar_batch.py`、`ocr_shadow.py`、`runner.py`、`runner_helpers.py`、`runner_lifecycle.py`、`state.py`、`slot_frame.py`、`sidecar_diagnostics.py`。
- 游戏内呈现：`src/hextech/interfaces/overlay/` 下的 `host_geometry.py`、`host_presentation.py`、`host_runner.py`、`host_render_state.py`、`host_data_preparation.py`、`renderer.py`、`host_presentation_smoke.py`。
- 桌面：`src/hextech/interfaces/desktop/window_presentation.py`、`background_runtime.py`。
- 打包检查：`tooling/build/package.py`、`tooling/acceptance/smoke_packaged_startup.py`。
- 新测试：`tests/test_overlay_held_scene_evidence.py`、`test_overlay_first_feedback_order.py`、`test_overlay_mss_capture_exclusion_native.py`。
- 既有测试增量：`tests/test_overlay_capture_roi.py`、`test_overlay_host_presentation.py`、`test_overlay_live_geometry.py`、`test_desktop_window_presentation.py`、`test_overlay_host_visibility_runtime.py`、`test_overlay_runtime_docs.py`、`test_package_rules.py`。
- 文档：本文、`docs/overlay-runtime.md`、`docs/overlay-recurrent-issues.md`。

### 验证记录及未通过范围

- `python -m pytest -q -k 'not native' --junitxml=.artifacts/r9-tests/non-native.xml`：1796 passed、19 subtests passed，31 native用例另行执行。
- `python -m pytest -q -k native --junitxml=.artifacts/r9-tests/native.xml`：29 passed、2 failed；两项失败均为前台保持断言，记录到 HWND 723622（ChatGPT.exe）与330774（Weixin.exe）之间变化，不是测试窗口。保留原始失败，不据此宣称原生门通过，也不改断言或猜测正式程序抢焦点。
- 最终定向 `test_overlay_host_presentation`、`test_package_rules`、`test_overlay_held_scene_evidence`、`test_overlay_first_feedback_order`：64 passed，报告 `.artifacts/r9-tests/final-focused.xml`。
- Ruff通过；Pyright明确使用本工作区 `.venv/Scripts/python.exe`：0 errors、1个既有 `__all__` 警告；无新增忽略规则。`git diff --check`通过。
- 独立审查发现按钮消失后的grace仍可能复用旧取证租约，现由 `next_held_scene_evidence` 清除，并用“合法hold→无按钮grace→按钮返回不得重建→正常scene重新确认”序列关闭；复核为代码GO，非真机GO。
- MSS捕获排除检查补强：不仅要求紫色marker消失，还须捕获到自有目标底色；黑/白/错误底色不能算成功。自有fixture底窗置顶以避免上层排除后捕获其他应用，生产窗口不受此改动影响；严格底色原生用例1 passed。
- 未取得新的四规格真实内框图，不修改或冒充校准结果；未运行真实League五局或双屏十次验收。
- 最终隔离候选：`.artifacts/r9/releases/HextechCompanion-20260909-r9b`，Build `20260909T114026Z-51cd79d6a7cc`；278个源码清单文件的指纹 `51cd79d6a7cceccc9eb42c2ca7220f386f8f6ed51ed0af25f5d58275d08907ee` 与当前源码一致。构建命令使用 `--verified-snapshot-root` 只读现有9月8日快照、`--artifacts-dir .artifacts/r9 --release-name HextechCompanion-20260909-r9b --smoke-root .artifacts/s9b`，未传 `--refresh-data` 或 `--deploy`。
- r9b打包退出码0；clean、stale_sidecar、populated_runtime三种隔离首启全部 `ok=true`，每种都通过同源MSS及真实底色排除检查。原生DLL最大路径193/259。旧r9候选只保留历史证据，不作为本轮最终候选；没有覆盖或清理旧包。
- 正式安装EXE/manifest、`.previous` manifest、唯一桌面快捷方式及真实current/checkpoint/slots状态已逐文件SHA-256核对不变。没有staging、commit、push或部署；仅构建器搬移本轮生成的staging候选并回收自身临时目录。

## 2026-09-07/08 增量背景

本增量只处理游戏内显示锚点隔离、OCR 调度、评价展示加工和真实结果验收。
桌面备战席、来源抓取协议、识别准入阈值、Stage/generation 固定语义不变。

## 显示锚点

- `interfaces/overlay/display_geometry.py` 独立持有显示卡框与统计内框，不读取 Vision ROI 常量。
- 四个目标尺寸及 720p 兼容尺寸保留本轮开始时的坐标；没有同规格真实内框证据，不猜测新坐标。
- 2560 宽统计使用 30px，1920 宽 23px；不通过缩字、压缩字形或删除指标让文字适配。
- `anchor_version/anchor_fingerprint/anchor_source/calibration_evidence_sha256` 进入布局诊断；目前资格仍为 `pending_real_device`。
- Host 重绘键包含实际 HWND、目标矩形、显示器、DPI 及显示锚点身份；诊断时间戳和置信度噪声不触发重绘。
- 所有支持规格的真实 Canvas bbox 通过，只证明程序内安全区容纳文字，不证明它与真实游戏卡片对齐。

## OCR 调度

- 生产 OCR 不受 observe 的 5 秒节流。限频中的 diagnostic 必须留在低优先级 mailbox，Condition 可被生产任务通知唤醒。
- 不得先把 diagnostic 出队再睡眠，否则后来的 production 会被同一个 worker 人为阻塞。
- 保留单 worker、生产逐槽 coalesce/优先执行、diagnostic latest-only、exact/唯一 ID/置信度/独立帧规则。
- 新增有限窗口的生产与诊断 queue-wait/inference 分段 P50/P95；不增加逐帧日志。
- 本次调度回归证明移除了人工等待，不能据此声称真实名称准确率达标。失败 ROI 仍须独立真值及 holdout。

2026-09-08 返回链增量：

- 生产完成结果进入容量 32 的 mailbox，不再依赖同一输入 SHA 的截图再次出现才能消费。保留提交时的 session、epoch、slot generation、frame ID、采集时间、模型输入 SHA 和感知 fingerprint。
- 结果只能原位升级 tracker 最近五次观察中的同一帧，输入 SHA 和 fingerprint 必须吻合；淘汰帧不重新插入，当前 miss 不变成额外一票。首次确认仍要求三个独立 OCR exact 观察。
- 模板 strong 冲突、跨槽候选/稳定身份冲突、过期/未来/非有限采集时间、旧 slot generation 和结束场景均不能借回流授权 READY。回流和当前帧一起归约，仅发布最终事件。
- 关闭时清空队列并拒绝迟到完成及后续提交。新增测试包含真实 observe→worker→mailbox→tracker 路径，不把单独 worker 计算完成等同最终正确呈现。
- 此增量不补猜原 ROI 缺失的字、不改变 exact/0.95/3-of-5 门，也不证明缺少全帧的左槽或游戏内锚点已经修好。

## 评价展示加工

- 原文先完成来源验证、身份关联、原始条目去重及投影覆盖统计，派生摘要不替换原始 content。
- 背景准备线程持有有限摘要缓存；原文哈希、规则版本和显示规格共同决定复用身份。
- 仅进行可审计的空白/标点规范、相邻完整重复句和明确无信息导航套话去除，不跨条件段落去重。
- 数值、单位、否定词、限定词、触发条件和推荐理由不得为满足字数而删除。
- 预算按具体面板、字号、行高和英雄/评级/标签占用计算；72 汉字只是一次 476×96px、18px 的总容量估计。
- 避免 renderer 的固定 180 字裁切和 Canvas 的无提示二次截行；未能安全收敛必须显式记录，完整原文仍可追溯。
- Pillow 使用 YaHei UI 对应 face 做后台预算，最终 Tk 实际 bbox 仍是独立复核，不假设两个字体后端逐像素一致。
- 无匹配、准备中、无来源统计、过期、超空间及禁入区漏画分别记录，不把它们都叫抓取慢或识别失败。

## 验收与证据

- 不根据 READY/OCR 结果自己生成真值。`tooling.acceptance.overlay_truth_probe` 只接受显式标注与图像/捕获绑定。
- 私有 timeline v2 增加 runner 原始 `captured_frame_id`，模板与 OCR 路径均可关联；旧记录不改写，缺失/零 ID 不能作为完整真值证据。
- 初次错误 READY 即使后来纠正仍算错误；未确认与超时样本保留，不能从成功率分母移除。
- 同一个捕获身份不得重复计数。静态卡片在不同真实捕获中可以有相同内容哈希，不能只凭像素相同判定它们是同一帧。
- `tests/test_overlay_display_geometry.py` 验证识别参数改变不能移动显示、Host 上下文变化使缓存失效。
- `tests/test_overlay_ocr_scheduling.py` 覆盖诊断限频抢占、latest-only、关闭唤醒；既有 OCR 合同继续全量回归。
- 正式性能门保持：反馈≤300ms，捕获+识别 P95≤180ms，首次完整正确三槽 P95≤900ms，event→present P95≤100ms。
- 不修改现行五局、碎片、重随、Alt-Tab、中途重启及独立样本数量门；默认不生成 PNG，诊断仍由唯一留存器管理。

## 后续联合稳定化审查（2026-09-08，未部署）

### 2026-09-08 双屏与异步回流收口（候选验证，不代表真机通过）

- Host 仍由 Sidecar/Context 绑定游戏身份，但每次绘制前只读核对该 HWND 的实时客户区；新鲜事件的旧矩形不再覆盖当前几何。严格查询失败不使用外框或旧矩形。移动/换窗立即刷新显示器上下文，尺寸或 HWND 改变先撤下旧 Canvas，再在新几何绘制安全 shell；纯平移不主动隐藏。
- `slot_frame` 固定顺序为：明确重随推进代际、登记当前独立捕获（稳定槽 miss 也占窗口）、回填原帧 OCR、跨槽与当前 strong 冲突裁决、统一发布。完成于本 tick 的结果不会因原帧尚未入窗而丢弃。
- 未 READY 槽也能推进重随 generation；旧票清空、旧 generation 完成结果不可回流。OCR exact 与模板 medium 分开计票；同一帧的模板 medium 只有拿到绑定一致的真实 OCR exact 才原位升级，不增加票数。稳定槽的替换同样使用最近五个独立观察，不能把稀疏成功帧拼成 3/5。
- 新增 20260907 名称回归原图：右槽“三重射击”可被现有模型 exact 识别；左槽右缘截断，不补猜末字。两图均属同一 session/epoch，只作 regression，不是独立 holdout；图像人工真值仍 pending，不能据此报告完整准确率。
- 现有显示内框坐标及字号不作无图校准。当前正式入口保持不变；需要候选五局与双屏实际画面，才能完成真实识别、P95 和位置验收。

下列既有发布风险已进入独立启动链修复，不把局部测试通过写成正式部署完成：

1. 已移除 `runtime_bundle._write_verified_snapshot_startup_status` 对抓取 checkpoint 的强写；播种只写安装/启动状态，不清空真实 pending。抓取周期与安装凭据分别验证。
2. `startup_refresh` 已恢复 30 秒并同步测试。恢复代以本次 Desktop 进程的 selection 凭据和完整 cohort 验证，不依赖延长等待。
3. 空闲 Host 的 bootstrap generation 由原单后台准备线程有界刷新；实际对局的统计固定代不因此更换。失效快照保留旧身份并报告错误，不能标为新数据。
4. 失败名称 ROI 新增有限几何元数据（原始 capture rect、frame size、title/icon/button box、transform 和采集时间）；仍未取得完整三卡真值前，不修改裁剪坐标或显示锚点。

本次用户授权的最终交付是本地打包、唯一部署器替换原桌面入口并保留 `.previous`；稳定化门未通过前不执行替换，不提供 ZIP 下载入口、不清理旧包或提交推送。
原始图像和人工真值不足时，位置校准及真实识别验收明确停在未完成，不能用 mock、说明图或启动 smoke 替代。
