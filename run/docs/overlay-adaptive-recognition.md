# 独立识别 Catalog 与有界选择证据

## 状态与范围

本文描述 2026-09-14 隔离工作树的当前源码候选，不是正式安装、最终打包或真实 League 验收报告。自动测试、独立刷新结果与真机选择正确率分别记账；五局稳定门、真实性能和窗口最终可见性仍待验收。旧 r10/r11/r12 与刷新历史文件不回写。

这里的自适应指版本检查、逐身份能力和受保护的证据反馈，不是自动学习身份、降低 READY 阈值或自动晋升模板。

真实历史截图留出实验发现，相关的两字体匹配仍能把未知名称配成旧身份。当前v2因此要求严格OCR确认：合成字体/图标不独立投票；只有身份三元组与人工实拍基线一致的observed-name保留原快路径。模型不变、OCR门仍为0.95 exact与3/5独立帧；缓存schema=6防止旧条目绕过准入。rf5是修正前已过冻结门的候选，不能当作此修正的验证产物。

## Catalog、能力与统计分离

- DataService 验证 worker 的 immutable Catalog 候选后独立发布 `var/catalog/current.v2.json`；worker 不自行 promote。Supervisor 从该发布结果读取识别数据，不等待 ARAMKit 全英雄统计，也不以 snapshot 是否存在决定词表可用性。
- 新 Catalog 的 `augment_identities.v2.json` 保存 metadata 权威身份与启用状态；`augment_assets.v1.json` 保存视觉资源。资源缺失不删除合法身份，统计缺失不删除合法身份。文字、图标与统计是独立能力，不能以一个通道缺失令全池失效，也不能把 metadata-only 身份伪称已有可用图标。
- 每个通道只建立具备该能力且通过校验的行。名称冲突、共享图标及缺资源保留明确原因；具备能力不等于允许单帧 READY，原有 exact/OCR、冲突及逐槽时序确认规则继续有效。
- 统计 snapshot 和 source unit 始终保留原 Catalog ID/provenance。新识别 Catalog 可以与旧但完整的统计并存，不把旧统计改写绑定为新 Catalog。
- 启动恢复先验证统计完整闭包；较新的独立 Catalog 必须另行完整验证后才可保留，不能因其与统计 Catalog 不同而一律回退，也不能信任未验证 current。快验 receipt 不满足时走完整验证；retention 同时保护独立 active Catalog 与统计闭包。

## 检查、采用与局中保护

2026-09-16 起，周期/条件请求/数据年龄口径以 [版本驱动刷新](version-driven-refresh.md) 为准。四小时只检测，来源未变且本地完整不默认全部更新；本节局中采用和矩阵保护继续有效。

- verified seed 首屏宽限 30 秒，宽限期间不由周期 due 抢跑。首次自动刷新提交 core 检查，不能继承 seed 的未来 due 而跳过本次检查；手动 core 同样检查独立识别 marker 和核心来源。
- Catalog 检查自己的 DataDragon/CDragon/模式 metadata marker，不借 ARAMKit marker 判断识别资源是否更新。force 表示强制检查，不表示无变化也全量重建。
- `checked_at` 是本次检查时间；ARAMKit `data_at` 是验证后上游 `buildTimeUnixMs` 对应时间，缺失时未知，不用本机产物完成时间冒充。内容未变显示“已检查，无变化”，不移动 previous 或伪造新统计代。Optional 的失败/完成独立呈现。
- 局中可检查 marker，昂贵 Catalog 构建在不安全负载下返回 `catalog_build_deferred_in_game`。该结果不是来源失败，也不标记已经完成更新；保留已有成果与旧识别矩阵，稍后重查。已在途静态请求不因普通游戏状态变化丢弃结果。
- 矩阵采用要求明确安全的非局中上下文；仅 selection inactive 不够，过期或未知上下文同样阻止切换。统计刷新不触发识别矩阵全量重建。Apex browser fallback 在请求前检查局中/暂停上下文；Mayhem 使用静态请求，不新增 browser/stealth 路径。

## 默认有界选择缓存与人工真值

2026-09-15 源码增量以 [运行手册 rf8 修复](overlay-runtime.md#2026-09-15-rf8-真机问题修复增量源码未部署) 为准。`raw_scene_evidence` 与 `final_classification` 分开，按钮/absent 矛盾不是垃圾帧。weak-only 不持续每两秒写入，队列同组最多 12 帧并保异常代表；低等级不能挤掉高等级存量，三槽首次完整 READY 耗时仅锁存一次。新私有持久化模块继续使用原缓存路径、schema v2 兼容读取与相同保护边界；自动采样始终 sparse，不能生成完整验收结论。

默认开启；只在显式设置 `HEXTECH_SELECTION_CAPTURE_ENABLED=0` 时关闭新缓存，以进行同条件开/关性能对照。它不关闭旧显式截图、时间线或识别，不改变ROI/阈值。关闭时最近缓冲手动保存不可用。

- `var/recognition/selection-cache-v2/` 默认消费现有捕获中的疑似选择区域，不新增完整游戏截图。缓存与成功 READY/selection epoch 独立，保留首帧、清晰帧、末帧及有限观察；manifest 带逐槽 `slot_rois` 几何/资产引用，供人工定位对应名称和图标。
- 新缓存只在自身目录治理：最多 200 组、256 MiB。人工、labelled/protected、未知、部分写入及外部引用等内容不当作可淘汰缓存；这些字节仍计配额。无安全淘汰空间时拒绝新写入并反馈容量状态，不借清理其他目录凑空间。manifest 最后提交，失败残留继续保护。
- `var/recognition/failure-inbox/` 保留旧有限失败证据和发生记录，不再由新生产入口写入或轮转。`evidence_starved` 继续保持公共 detecting，不变成硬失败，也不放宽阈值。新缓存写入失败不改变身份、READY 或 revision。
- 显式旧 ROI debug dump、timeline、人工 case/truth 与新选择缓存是不同载体。不得删除或改写历史真值，不以生产 OCR/模板输出生成自己的 truth，不自动把采集图晋升为 exemplar。人工确认与独立 holdout 验证仍是模板采用前提。

## 人工真值与只读回放

`python -m tooling.diagnostics.selection_replay --group <组目录> --truth <人工标签.json> --catalog-id <已保存Catalog ID>` 校验图片哈希与物理选择区坐标，再用指定Catalog的生产模板候选规则回放。标签绑定 `diagnostic_id`、原 `manifest_sha256`，`frames` 必须逐项含 `frame_index` 和三槽 `expected_slots`（canonical ID或null表示没有可确认身份）。修订标签保存为新文件，旧标签可保留和复核；工具不写生产模板或修改原图。

此入口报告单帧模板候选及严格OCR证据的错误和未知率，OCR只使用记录中有效的槽位坐标；不运行实时异步队列、不复制稀疏样本凑时序，因此明确 `temporal_acceptance=false`、`qualified=false`。完整时序验收仍使用既有独立捕获时间线与人工选择区间。原图缺失、标签覆盖不足或坐标不完整直接拒绝，不猜测名称或ROI。

显式追加 `--export-candidate <当前运行根/recognition/corpus/samples/新目录>` 才导出人工绑定的名称裁图和内容寻址候选清单；已有目录拒绝覆盖，不触碰生产assets。更正人工绑定生成新候选版本，旧版本不自动应用，仍须独立holdout审查后明确采用。

## 验收仍未闭合

必须独立核对真实 Build/PID/目录、上游检查结果、统计实际年龄、同一游戏实例中的不换矩阵、逐槽 ROI 对应、未知/人工资产保护，以及错误 READY/碎片/重随/Alt-Tab/最后可见像素。合成帧、单元测试和 packaged smoke 不替代这些结果。
