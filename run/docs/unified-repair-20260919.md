# 2026-09-19 run 统一修复验证记录

## 范围

在现有 main 的 run 工作区修改，无分支／worktree 创建，无 staging／commit／push，无正式部署或快捷方式变更。根仓既有五处改动不属于本轮。正式安装、用户运行数据、历史 source/快照、人工真值及回滚安装未改写或清理。

## 已修复与保留

- Mayhem 默认投影从统一目录读取 entries 和英雄 ID 映射，不再要求顶层数组；生产形态 fixture 已验证修复前失败。验证错误归类、处理版本及失败指纹随之更新。
- 新刷新和新快照不再包含 Blitz，旧格式完整性校验仍保留。推荐链为已验证 ARAMKit Stage→同英雄 all→明确缺失，局中冻结不变。
- 原生 Tk 实测重复字体测量是热路径热点，增加有界缓存并提供绑定事件的分段耗时。识别阈值、字大小和统计锚点未变。
- 新增有界检查／HTTP 证据；缺计数不冒充完整失败率。联动无来源、明确空与无匹配分开。
- 选择证据缓存轮转、保护项、同组 12 帧合并和重复 PNG 复用继续保留；补容量及拒写反馈、重复日志合并，未清理现存证据。

## 修改文件

共 33 个路径，以下相对 run；包含新增测试、验收工具与文档。

- `docs/overlay-runtime.md`
- `docs/version-driven-refresh.md`
- `src/hextech/bootstrap/data_service_application.py`
- `src/hextech/infrastructure/persistence/diagnostic_retention.py`
- `src/hextech/infrastructure/sources/aramkit/http_response.py`
- `src/hextech/infrastructure/sources/aramkit/incremental_projection.py`
- `src/hextech/infrastructure/sources/aramkit/service.py`
- `src/hextech/infrastructure/sources/mayhem/service.py`
- `src/hextech/infrastructure/sources/refresh_policy.py`
- `src/hextech/infrastructure/sources/refresh_service.py`
- `src/hextech/infrastructure/sources/refresh_service_schedule.py`
- `src/hextech/infrastructure/vision/failure_evidence.py`
- `src/hextech/infrastructure/vision/selection_capture_persistence.py`
- `src/hextech/interfaces/overlay/canvas_renderer.py`
- `src/hextech/interfaces/overlay/data_notice.py`
- `src/hextech/interfaces/overlay/host_presentation.py`
- `src/hextech/interfaces/overlay/host_render_state.py`
- `src/hextech/interfaces/overlay/host_sync.py`
- `src/hextech/interfaces/overlay/renderer.py`
- `src/hextech/interfaces/overlay/text_metrics.py`
- `src/hextech/modules/recommendation/service.py`
- `src/hextech/modules/recommendation/stage_projection.py`
- `tests/test_incremental_projection.py`
- `tests/test_incremental_refresh_service.py`
- `tests/test_mayhem_refresh_health.py`
- `tests/test_overlay_stage_projection.py`
- `tests/test_recommendation_source_freshness.py`
- `docs/unified-repair-20260919.md`
- `src/hextech/infrastructure/observability/refresh_attempts.py`
- `src/hextech/infrastructure/sources/aramkit/http_metrics.py`
- `tests/test_unified_repair.py`
- `tooling/acceptance/verify_unified_repair.py`
- `tooling/diagnostics/overlay_draw_benchmark.py`

## 证据与命令

所有临时输出位于 `run/.artifacts/`；路径以 run 为基准。

- 定向回归：`python -B -m pytest tests/test_unified_repair.py tests/test_dev_gate_structure.py tests/test_incremental_projection.py tests/test_incremental_refresh_service.py tests/test_overlay_stage_projection.py tests/test_recommendation_source_freshness.py -q -p no:cacheprovider`。阶段记录 `repair-20260919/focused-v2.xml` 为 131 passed；随后新增 HTTP／容量回归，`test_unified_repair.py` 单独为 15 passed。
- 全量回归：`python -B -m pytest -q -p no:cacheprovider --tb=short --junitxml=.artifacts/repair-20260919/full-v2.xml`，2500 passed、19 subtests passed，317.02 秒。45056 条 warnings 保留在日志，主要为现有 Pillow `getdata` 弃用警告；本轮未以过滤警告冒充零警告。
- 静态检查：`python -m ruff check src tests tooling` 通过；`python -m pyright` 为 0 errors、1 个原有 `sidecar_diagnostics.py` 的 `__all__` 警告；`git diff --check -- .` 通过。
- 性能对照：`python -B -m tooling.diagnostics.overlay_draw_benchmark --output .artifacts/repair-20260919/draw-benchmark.json`。同一隐藏 Tk、三槽联动、40 帧变更统计：无缓存热 P50/P95 32.67/41.77 ms，有缓存 1.93/3.44 ms。先后运行的冷启动受系统字体缓存影响，不作为独立冷启动收益结论。此结果不是游戏端到端延迟。
- 隔离网络：`python -B -m tooling.acceptance.verify_unified_repair --source-root <正式var，只读> --output .artifacts/fix19-runtime`。只复制通过来源闭包验证的公开数据，不复制账户、令牌、用户配置或截图。联网及新快照发布只在 clone 中执行。
- 隔离结果 `fix19-runtime/verification.json`：ARAMKit 当前版本 `data/16.18-20260918-77f3e6d23914`，173 个排行及英雄 38 的 all/stages 验证成功；Mayhem 668 条输入、550 条 added_items；新快照包含 173 英雄，来源为 catalog/aramkit/apex/mayhem。
- 联动投影：124/124 个 Catalog 可解析名称成功投影；仍有 4 个源名称不可映射（包括乱码和非标准名称），仅诊断，不生成伪造联动。这不等于所有来源名称都完整覆盖。

## 候选与验收边界

候选从隔离 clone 的新快照构建，使用独立 artifacts 与 smoke 根，不使用 `--deploy`。

- 命令：`python -B -m tooling.build --verified-snapshot-root .artifacts/fix19-runtime/snapshots --release-name HextechCompanion-20260919-repair --artifacts-dir .artifacts/fix19-package --smoke-root .artifacts/fix19-smoke`，环境 `HEXTECH_VAR_DIR` 仅指向该隔离 clone。
- 候选：`.artifacts/fix19-package/releases/HextechCompanion-20260919-repair/`；同级 zip 已生成。
- Build：`20260919T103700Z-0729d230179e`。320 个源码文件的当前 fingerprint 与候选 manifest 一致：`0729d230179e3f4dab57805d07ea7eba639f33696a3a84b3be8700a8fe928fae`。源码未提交，不能仅凭 manifest 的 base revision 判断此次修改。
- `repair-20260919/build.log`：packaged startup 总体 `ok=true`，clean、stale_sidecar、populated_runtime 三个 fixture 均 `ok=true`，含 Desktop/Overlay 原生呈现 smoke。
- 构建器仅回收其自建临时构建目录和 smoke 进程；候选、隔离 clone、验证记录保留，未清理正式 runtime 或历史证据。

真实游戏的五局、首次三槽 P95≤900ms、事件到呈现 P95≤100ms、捕获＋识别 P95≤180ms 尚未由新候选完成。正式安装仍运行旧 Build；内部 READY、隐藏 Tk 与 packaged smoke 不替代用户看到的实际结果。
