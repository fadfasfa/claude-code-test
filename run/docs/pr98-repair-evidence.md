# PR #98 修复证据与验收边界

## 四项审查修复

- 局中 pending Catalog 仅禁止采用新识别矩阵，不阻止旧 Sidecar 恢复。恢复要求旧 Catalog、
  fingerprint 齐全；统计来源 origin generation 可合法为空（Catalog-only 路径）。
  `802d126`曾错误要求三字段齐全，复审后纠正；缺Catalog/fingerprint仍明确失败。
  Supervisor 不再把失败标成恢复成功。
- 新 ranking 使用投影器同一验证路径生成英雄集合，锁内排除已移出的旧英雄贡献，发布失败恢复旧集合。
- 成功 commit 后登记保留治理 pending，Core/Optional/worker 均空闲才执行。current、previous、
  journal、recovery、Host冻结代、Sidecar识别引用和在途候选闭包均保护；坏引用、reparse/junction、
  不完整provenance全局安全跳过；治理失败不能将已commit刷新改判失败。
- v3 seed分别验证、收集统计Catalog与独立识别Catalog。安装、部署校验、Sidecar smoke与启动receipt
  分别绑定两套身份；旧单Catalog seed缺识别字段时沿用原身份。receipt写入完整验证，加载仍使用既有
  header摘要与全文件元数据失效机制，不在快路径重复全量哈希识别图像。
  写入只复用前后比较通过的元数据快照；最终比较后发生的文件漂移不能被重新读取并登记为可信。

## 工程验证工具修正

全量测试暴露两个既有桌面Z-order验收假阴性：原helper只遍历128/256个窗口，而本机离屏fixture
的Panel实际位于普通blocker之前第328个窗口。透明自有窗口复现证明Panel为topmost且位于上方。
现改为按当前顶层句柄快照给出有限预算，再逐个GW_HWNDPREV验证顺序；循环、句柄缺失、快照增长
超预算仍报错。未修改生产前台租约、置顶策略或隐藏门；补402窗口正负用例及原生异常回归。

## 802d126 复审：两个 P2 的生产链路回归

此前Sidecar身份解释有误：独立识别采用Catalog B后，生产`CatalogVisionDataSource`给模板构建的
production pool也绑定B，所以状态中的`catalog_generation_id`和`recognition_catalog_id`都是B。
统计snapshot的Catalog A、pool ID/count只用于其完整闭包验证，不能再约束Sidecar矩阵。

本次smoke/deploy共用识别验收函数：先校验当前B的pointer、manifest与Catalog文件，再用生产
`_catalog_production_pool`计算预期pool ID/count/full_catalog_count，并以B的数量校验矩阵能力。
旧单Catalog seed缺独立识别字段时沿用旧合同；统计闭包的完整校验不放宽。
Catalog正文损坏转换为明确验收失败，不能穿透错误收集，更不能启动不可信Sidecar。

新增fixture实际创建统计A与识别B（pool ID及身份数量均不同），调用生产Catalog读取、模板矩阵构建和
状态生成函数后送入smoke/deploy。只替代子进程外壳与启动session凭据，不手填Catalog/pool身份。
同一fixture在内存中换回`802d126`的原smoke/deploy函数，两项均失败；新函数通过。
错误Catalog、旧pool ID、旧数量、矩阵缺失、pointer及正文漂移分别拒绝。

Catalog-only测试从不存在统计snapshot的runtime开始，真实Catalog读取与模板构建，经首次启动记录A，
随后发布B并真实预热B，再验证局中仍用固定A及旧fingerprint重建，origin保持空。
缺Catalog或fingerprint仍fail closed；未修改OCR阈值、统计口径或布局。

本次复审修复最终验证：`python -m pytest -q --junitxml=.artifacts/refactor/pr98-review/pytest-p2-final.xml`
为 **2485 passed，19 subtests passed**；Ruff通过，Pyright 0错误/1个既有`__all__`警告，diff-check通过。
测试覆盖进程替身下的真实Catalog读取/模板构建，不替代重新打包、部署或游戏内呈现验收。
修改8个文件：`vision_handoff.py`、`tooling/build/deploy.py`、新`tooling/build/recognition_contract.py`、
`tooling/acceptance/smoke_packaged_startup.py`；`tests/test_package_deployment.py`、
`tests/test_recognition_catalog_handoff.py`、新`tests/test_sidecar_recognition_acceptance.py`，以及本文档。

## 统计文字碰框（2026-09-16 样本）

原始游戏截图尺寸为 2560×1440，SHA-256 为
`6d2e07eecf87dcff4f8f9d3f8b4efecc4e23c4fea4c7db476d8fe9a9c9f377ea`。
手机照片尺寸为 4096×3072，SHA-256 为
`f7c4531db7544bdaf2814cb74c9e347248ae3dd1d11e1bda98953c9f2b5f46e6`。
原图不包含 Overlay，照片用于确认碰框方向；二者不是同一时刻的同步帧。
原始图片保留在用户本地，不随代码提交。

在原始截图 y=790/820/840/850/860/875 的逐像素检查中，三槽连续暗色内部
（RGB 各通道均小于65）分别为 x=625–969、1115–1459、1605–1949。
y=890 时已收窄到 637–958、1127–1448、1617–1938，说明底部斜角开始侵入。
旧统计矩形中心为 (745,898)、(1289,898)、(1835,898)，左右偏离真实中轴，且靠近斜角。

新统计矩形为 (631,780,964,876)、(1121,780,1454,876)、(1611,780,1944,876)。
它们横向在实测直边内缩6px，底部在斜角之前；不修改识别ROI、联动区域或统计口径。
字体保持30px Microsoft YaHei UI bold；优先收紧空格，不能完整容纳的双指标才分两行。
例如常规48.5%/1.2%的实际字宽由338px经细间距调整为318px；
最大100.0%/100.0%即使无空格仍359px，不能通过缩字或裁掉数字解决。
生产Canvas当前每段文字只有一个item，没有另画文字阴影；以后增加阴影时必须纳入边界检查。

`display-anchors-v2` 只授予本张1440p样本截图校准资格，状态仍为
`screenshot_calibrated_pending_presentation`。其他规格、边框种类、游戏UI变体及修复后的
真实可见呈现不因此获得验收。16:10、1080p等保持既有待验收基线。
原生Tk测试核对实际bbox、字号、DPI100/125/150/175/200%、常见及极端数值。
本地离线叠图明确标注模拟，不是修复后游戏实拍或性能证据。

## 慢识别证据边界

rf10日志中20:40:04.290进入detecting，04.614已有两槽READY，12.200三槽READY。
最后一槽等待导致内部全槽READY耗时7.910秒；这不是event→present，也不包括场景准入前的时间。
对应帧级时间线已轮转，不能从现存日志确定是ROI、exact拒绝、独立有效帧不足还是失效重试。
不据此改0.95 exact、3/5独立捕获、碎片否定、跨槽冲突或观察任务5秒限频。

本地证据根为 `run/.artifacts/rf9-user/Local/HextechNexus/var`（只读）。
`logs/hextech_runtime_summary.log:818–821`记录上述Host状态；
`state/session_evidence/overlay-0675c0ae36b8e5379e0998225137ef1e-e23-r2-c46-4ee0ce914abd.v2.json`
在20:40:12.221934记录同会话epoch23的三槽最终结果：坦克引擎、急速之追求、溢流，
均为 `ocr_exact_fallback`，置信度分别为0.998529、0.999023、0.999871；
统计generation为 `20260916T064355-385a0a5c17`。最终快照不能证明等待期间的拒绝原因。
`state/diagnostic_retention.v1.json`记录timeline只保留20个epoch，现存最早为21:44:50，
故无法将目标20:40事件与逐帧OCR/缓存命中记录完整关联；不以稍后其他局的数据替代。

本次增加 `vision_epoch_summary`：仅在epoch终态成功写入时记录一次到既有轮转日志，
包括逐槽首次内部READY耗时、此前最后一次pending的拒绝原因/独立帧计数/OCR状态、
捕获及识别P50/P95，并标记timeline截断。摘要不保存原始OCR文字或截图，不产生逐帧日志。
此计时从首个合格捕获到内部READY，不冒充Host实际呈现或完整event→present指标。
自动测试覆盖两槽先就绪/最后一槽延后、重复终态仅一条日志、暂停探针排除、截断标记和脱敏。

性能验收仍需捕获+识别P95≤180ms、首个完整三槽呈现P95≤900ms，以及至少20个独立事件
event→present P95≤100ms。缺帧、失败和截断不得排除后宣称通过；本次自动回归不替代真机门。

## 802d126 自动验证结果（历史）

2026-09-17 `802d126`源码回归（不覆盖后续P2补充；未运行真实网络刷新或重新打包）：

- `python -m pytest -q --junitxml=.artifacts/refactor/pr98-review/pytest-final.xml`：
  **2475 passed，19 subtests passed**；JUnit合计2494项，0失败、0错误、0跳过。
- `python -m ruff check src tests tooling`：通过。
- `python -m pyright`：0错误，1个既有 `__all__` 警告。
- `git diff --cached --check`：通过。
- Windows原生Tk、完整自有窗口presentation smoke、402窗口Z-order边界、双Catalog及receipt、
  恢复Supervisor集成、retention故障与保护资产测试均包含在最终全量中。
- 早一轮全量的3项失败为旧单行断言及128/256窗口遍历假阴性，修复后定向13项及最终全量均通过。
  测试输出仍含既有Pillow/Starlette弃用警告，不作为本轮新故障处理。

## 发布边界

本轮仅修复代码、测试与对应合同，普通推送PR #98。未部署、未替换快捷方式、未清理旧包、
未合并PR。保留治理只能在隔离测试数据上验证，不对正式运行数据手动执行清理。

## 修改文件索引

以下均相对于登记工作区 `run/`；没有修改其他业务工作区或提交用户原始图片。

| 分组 | 文件 |
| --- | --- |
| 恢复 | `src/hextech/bootstrap/supervisor.py`；`src/hextech/interfaces/overlay/runtime_manager.py`、`vision_handoff.py` |
| 增量刷新 | `src/hextech/infrastructure/sources/refresh_service.py`、`refresh_service_lifecycle.py`、`aramkit/incremental_projection.py` |
| 存储与seed | `src/hextech/infrastructure/persistence/retention.py`、`cohort_seed.py`、`cohort_seed_catalog.py`、`cohort_validation_receipt.py` |
| 显示 | `src/hextech/interfaces/overlay/display_geometry.py`、`canvas_renderer.py`、`text_metrics.py` |
| 诊断 | `src/hextech/infrastructure/vision/sidecar_diagnostics.py`、`epoch_diagnostics.py`；`tooling/diagnostics/overlay_snapshot.py` |
| 验收与构建 | `src/hextech/interfaces/desktop/presentation_smoke.py`；`tooling/build/cohort_seed.py`、`deploy.py`；`tooling/acceptance/smoke_packaged_startup.py` |
| 数据测试 | `tests/test_cohort_receipt_v3.py`、`test_cohort_v3.py`、`test_incremental_projection.py`、`test_incremental_refresh_service.py`、`test_retention.py` |
| 显示与恢复测试 | `tests/test_desktop_foreground_layer.py`、`test_desktop_presentation_smoke.py`、`test_overlay_display_geometry.py`、`test_overlay_display_modes.py`、`test_overlay_safe_area.py`、`test_overlay_vision_timeline.py`、`test_recognition_catalog_handoff.py` |
| 打包测试 | `tests/test_package_deployment.py`、`test_packaged_smoke_isolation.py` |
| 合同与证据 | `docs/overlay-runtime.md`、`docs/pr98-repair-evidence.md` |
