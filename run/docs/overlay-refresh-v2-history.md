# 已退役 V2 刷新合同：冻结历史

以下段落来自2026-09-14同步前的 overlay-runtime/system-design，仅保留历史推理、旧回归与相对引用，不是当前生产指令。旧六文件已移至 `tests/support/legacy_refresh/`；不得从生产导入或恢复第二套引擎。

当前合同见 [overlay-runtime.md](overlay-runtime.md)、[system-design.md](system-design.md)，案例证据见 [overlay-case-baseline.md](overlay-case-baseline.md)。特别是游戏中统一取消、对局结束后等待30秒恢复、首session即锁代、自然年龄清空数值及四来源全量发布口径，均不能据此覆盖当前增量合同。

- ARAMKit 四字段 marker 与共享绝对时效共同决定英雄总体及 Stage 1–4 是否刷新：marker 相同只允许在 `last_success_at`（缺失时回退 manifest `completed_at`）年龄不超过 5 小时时复用，时间非法或超过 5 小时必须完整抓取；marker 变化时 Catalog、ARAMKit、Blitz、Apex、Mayhem 全部进入同一刷新周期。Overlay 优先显示当前英雄当前 Stage 的 ARAMKit 胜率/出场率，缺 Stage 行时回退同英雄 `all`，再缺失才显示 Blitz tier；Blitz 不提供胜率、选择率或样本量，禁止从 tier 推断百分比。production pool 仍只由同一 Catalog generation 的 `augment_assets.v1.json` 决定，任何统计来源都不参与 eligibility。generation 发布前逐 ID 核对 pool 的 `canonical_id`、路径和 SHA-256；即使 marker probe 网络失败，也不能晋升混合代。

---

- ARAMKit 为 fresh 而 Blitz 只能复用同 Catalog 的 verified last-good 时，generation 仍以 `data_status=fresh` 提供阶段/全量百分比，聚合 `health=degraded`、`data_reason=optional_source_stale`；Blitz 状态必须为 `last_good/data_stale/production_coverage_insufficient`，Canvas、session report 的公开行和公开 DTO 均清空 tier/rank/score。只有 ARAMKit 本身 stale 或失败时聚合状态才是 `data_stale`。

---

- 已通过 scoped manifest/path/size/SHA-256 校验的 ARAMKit 记录即使 `last_good/data_stale` 仍可用于 lineage 和诊断，但 Overlay 必须 fail closed：清空胜率、出场率、排名和 tier，不得把过期数值伪装为当前统计；显示 `统计数据为 N 小时前`、`统计数据为 N 天前`，时间缺失/未来/不可解析时统一显示 `统计数据暂非最新`。原始 stats、sample count、generation、run、scope 和 `data_at` 只保留在 recommendation DTO 与 session report。只有来源明确 fresh 时才允许 Canvas 绘制百分比。

---

- `DataSnapshotView.status(now=None)` 每次读取都按共享的 `SOURCE_INTERVALS × 1.25` 策略重新投影各来源绝对时效，优先使用 `data_at`，旧 generation 缺失时回退 immutable manifest 的 `created_at`；ARAMKit 的实际边界为 5 小时。超过边界只设置 `data_status=data_stale`、缺少更具体原因时设置 `data_reason=source_data_expired`，并记录总年龄 `stale_age_seconds`；不改写 manifest、`health`、`degraded_sources` 或表示 lineage 的 `freshness`。推荐、阶段统计、session report、桌面状态和 Overlay hint cache 都消费同一实时投影；桌面“数据 X 前”优先使用 ARAMKit `data_at`。Apex/Mayhem 的来源状态仍只影响联动区域，不污染胜率和出场率。

---

- DataService 的构建顺序固定为“ARAMKit scoped artifact → Blitz 海克斯 tier 排名投影 → Catalog 补全名称、稀有度 tier、图标和最终身份集 → 当前 generation 联动投影”。Host 在 game session 首次出现时固定 `stats_generation_id`，同局后台换代只记录 `new_stats_generation_id`，下一局才采用；stats-only 换代不重建 Vision matrix。Host 从同一 Live Client 请求读取 `championName + level`，按 3–6/7–10/11–14/15+ 解析 Stage 1–4；等级缺失时仅使用本局已确认的 0–3 次选择推导下一 Stage。每个 epoch 最多等待两秒并固定 champion、Stage、ARAMKit run 和单英雄 view，下一 epoch 可重算阶段但继续使用本局 generation。单英雄文件按 index path/size/SHA-256 校验，LRU 容量为 2。阶段只在游戏窗口右上角显示单行 `阶段 N`，卡片不拼接阶段、样本、综合回退或 freshness 文案。`sample_count < 100` 时胜率仍显示、出场率改为真实 `出场数 N`；`100–999` 保留两个百分比；两档当前阶段统计用柔和红 `#F87171`。`sample_count >= 1000` 的当前阶段统计沿用金色。`stats_scope=all` 的综合回退始终用蓝色 `#3FA9DC`，低于 1000 时再叠加红色细内框；缺少或非法 `sample_count` 时保留百分比并不推断低样本。Blitz 的 `source_tier` 是全局排名，`champion_tier` 只在来源列出的最多五个英雄中存在，当前用“该英雄 Tn · 全局 Tn”或“全局 Tn”展示。

---

- Apex 与 Mayhem 作为同一联动 cohort 原子晋升；单侧真实失败时共同保留 last-good。恢复后允许复用与当前 Catalog、manifest 和 artifact 哈希一致的已保存候选，不得手工改写正式 pointer。

---

性能报告只把同一目标 Build、同一 Sidecar instance 且同时满足 `selection_type=hextech`、至少一帧 `scene_state=active`、至少一帧 `selection_window_active=true` 的 epoch 计为合格 Hextech epoch。`candidate`、`body_shard`、`blocked`、纯 pause、`gameflow_ended` 和不足一次 active 的临时 epoch 必须从三槽覆盖率、首次 Canvas 与 recognition P95 中排除，并分别写入 `excluded_epochs_by_reason`。报告同时输出 capture、recognition、capture+recognition total 三段，保留每 session 最慢 10 个 observation 的纯结构化 `matching_timing`，不增加图片。不得通过排除慢帧、修改时间戳语义或减少真实样本伪造通过。

---

## 对局期间的数据刷新

存在可用 current generation 时，对局期间延后 Catalog、ARAMKit、Blitz、Apex、Mayhem 的自动刷新和手动核心刷新，状态为 `refresh_state=deferred`、`deferred_reason=game_in_progress`。这不是 `data_stale`，不计入失败和 backoff。Desktop 标题栏“刷新”调用 `POST /v1/actions/refresh` 的 `scope=core, force=true`；空 body 仍是旧的 due check。核心强刷不会无条件抓取 Optional，但正常到期的 Apex/Mayhem仍同行，ARAMKit marker 或 Catalog 变化会扩展为完整同代刷新。

刷新门由 DataService 的独立三态探针提供，不以 Host visibility 作为唯一事实源：Live Client 2999 或 LCU gameflow 明确 `InProgress` 时在局中；接口 unknown 但 `League of Legends.exe` 或游戏窗口存在时保守按在局中；只有无游戏进程/窗口且 LCU 明确非对局或不可用时才允许刷新。worker 运行期间每不超过 50 ms 读取 cancel signal；游戏门、Desktop shutdown 和 hard timeout 分别写 `cancel_reason=game_in_progress/shutdown_requested/hard_timeout`。取消先给 2 秒协作退出窗口，再关闭 Job Object 回收完整进程树；游戏取消不写来源失败/backoff，shutdown 不发布新 candidate 或 generation。

当前正式活动 Catalog 独立刷新：ARAMKit fresh 后可以发布新 generation；Blitz 失败时只能复用同 Catalog 且哈希验证通过的 last-good 并 fail-closed 隐藏排名，Apex/Mayhem 必须成对复用同 Catalog last-good。ARAMKit 失败不发布新 generation。Catalog 内容 SHA 变化时禁止两阶段混代，所有依赖来源必须绑定新 Catalog 后才能整体晋升。

若 fresh ARAMKit 上游出现活动 Catalog 未登记 ID，默认仍整轮拒绝；唯一例外是该 ID 同时存在于已验证的 blocked adoption Catalog。此时只把这些 adoption-only 行从活动 artifact 的 `all/stages` 投影中剥离，并在 ARAMKit manifest/report 的 `compatibility_filtered_augment_ids` 记录精确 ID；任意未被 adoption 证明的未知英雄/海克斯、重复、非法 rate、空 stage 或错绑仍 fail closed。严格全链允许活动 Catalog 显示为 `adoption_held`，但 ARAMKit 仍必须在 4 小时内 `fresh/fresh`，generation 的 degraded 来源只能属于 Blitz/Apex/Mayhem 且必须 last-good/data-stale。

Blitz 采用同一窄兼容合同：只有已验证 blocked adoption Catalog 能证明、但活动 Catalog 尚未收录的海克斯 ID 才从排名 artifact 过滤；过滤后必须重算 artifact 的 `record_count` 与 canonical content SHA-256，并在 manifest/report 精确记录 `compatibility_filtered_augment_ids`。这不会切换活动 Catalog、恢复 adoption lane 或扩大 production pool；任何未被证明的未知海克斯、未知英雄、schema 错误和过滤后 production 覆盖不足仍拒绝整个候选。

`refresh_checkpoint.v1.json` 只记录当前活动 Catalog 的发布周期；`catalog_adoption_checkpoint.v1.json` 独立保存待采用的新 Catalog、已完成 candidates、真实 pending、失败原因与 backoff。首次读到 Catalog 不同且没有 core generation 的旧 `full_catalog_rebind` 时，原证据原子复制到 adoption checkpoint 并标记 blocked，原 checkpoint 标记 migrated 而不删除；`pending_sources` 始终等于 due 减 completed。活动 checkpoint 只为仍 pending 的来源保留 `reason_code/failure_stage/error_type/fallback_used/last_good_available/diagnostics`，完成来源的失败证据必须消失；诊断有深度、数量和长度上限，并剔除 traceback、命令行、环境、proxy、credential、token、cookie 与 secret。新 Catalog 只有 Blitz 覆盖至少 95% 且 ARAMKit、Apex、Mayhem 全部同 Catalog 后才能整体采用，普通 active refresh 不会续跑或晋升 adoption lane。

若刷新过程中进入游戏，Core 发布前取消 worker 并保留 checkpoint；Core 已发布后只取消或延后 Optional，已发布 generation 不回滚。checkpoint 与延后门保留原请求的 `scope/force`，对局结束后等待 30 秒只恢复一次等价请求。`GET /v1/status.refresh_status` 暴露 `state/scope/phase/reason_code/generation_id/pending_sources/started_at/completed_at`；Desktop 对 running/deferred 使用粘性文案，对 completed/unchanged/failed 显示 6 秒。没有可用 snapshot 的冷启动不受此策略阻塞。

Blitz marker 相同时也必须服从共享绝对时效：verified pointer 的 `last_success_at` 年龄在 2 小时 30 分以内才可返回 `not_stale`，恰好边界允许复用；超过边界、时间缺失或非法时完整抓取。所有来源与严格 verifier 共用 `SOURCE_INTERVALS × 1.25`，不再重复定义 Apex/Mayhem 阈值。Snapshot manifest 的 `health/degraded_sources` 仍是 immutable lineage；消费者数据读取时过期合并进 `effective_degraded_sources`，Catalog 由 `adoption_held` 独立门处理，Desktop 与严格门优先消费 effective 字段。

Blitz 的公开 JSON 仍由 Scrapling 静态 `get` 主抓；只有最终归类为 `tls_error` 或 `network_error`、host circuit 未打开且总预算仍有剩余时，才允许一次 `requests` 静态 fallback。403、429、schema/identity/coverage 错误、无效 payload 与 2 MiB 超限都不得 fallback；主路径和 fallback 都执行相同大小门与 TLS 校验。manifest/report 必须记录实际 `fetch_backend`、`fallback_used` 和 `fallback_from`，但不得引入 browser、stealth、代理、Firecrawl 或旧 `run/crawler`。

冻结包若已安装完整 verified bundle seed，DataService 必须先发布该 generation，再给首次自动远端刷新 30 秒启动宽限；手动刷新不受宽限限制，宽限到期若已进入游戏仍走既有 `game_in_progress` 延后/取消。Blitz 失败按 optional stale 保留同 Catalog last-good；ARAMKit 失败保留既有 current 供降级显示，但不发布 generation，也不能满足新候选 fresh-data 构建门。



---

使用 `--verified-snapshot-root` 构建时，bundle 除 snapshot seed 外还必须携带 `resources/cohort-seed`：当前 Catalog generation（含 content-addressed 图标和 `augment_assets.v1.json`）、ARAMKit/Blitz/Apex/Mayhem immutable run、六个 current 所需绑定与同代 schedule。冻结 Desktop 在 runtime logging 与首屏就绪后、启动 DataService/Supervisor 前安装该 seed，复用相同哈希文件，最后提交 snapshot pointer；所有子角色禁止 seed。不删除旧 generation 或历史用户数据。`--refresh-data` 成功时必须以实际刷新后的 `var/snapshots` 为打包输入，不能继续打旧 `resources/seeds`。

---

完整验证成功后写入 `cohort_validation_receipt.v1.json`。只有 Build/source fingerprint、current pointer、generation manifest、全部验证文件的 size/mtime 与 validator contract 完全一致且 promotion journal 不存在时，Desktop 才可走 metadata 快路径；任一漂移立即回退完整哈希。真正 promotion 重新写 receipt，写失败只影响下次启动。刷新候选的 Catalog、四来源 immutable pointer/provenance、payload 与 production pool 内容身份完全不变时，返回 `promotion_disposition=unchanged`，不创建 generation、不移动 previous、不打开 promotion journal；`checked_at`、`stale_age_seconds` 和本轮 refreshed sources 不参与 immutable generation 身份。

---

## 来源门禁

Catalog 以 Data Dragon 的普通英雄条目为事实源；`id` 以 `Jade_` 开头的模式变体在构建阶段明确排除，不以任一统计来源的现有覆盖做反向交集，因此未来普通新英雄仍可进入 Catalog。`catalog.result.json` 的 `source_filter` v1 记录上游条目数、规范条目数、排除数、原因计数和最多 10 个数值 ID 样本，便于区分上游扩展与解析回退。

英雄总体统计继续使用 ARAMKit 的公开静态 JSON，固定请求 `dataset=all`。一次 run 先读取
`data/versions.json`，再读取排行和逐英雄详情；详情默认并发 6、硬上限 8，只有 timeout、TLS、network 与 5xx 进入一次并发 2 的尾部重试。单响应 UTF-8 内容上限 32 MiB、整轮累计上限 2 GiB，403/429 立即熔断；worker 总预算 10 分钟。解析后保留英雄概要与 `augments.all`、stage 1–4；generation 主 payload 不复制阶段行，Overlay 通过固定 provenance 的单英雄 scoped view 按需读取。

ARAMKit run 的开头和结尾必须得到完全相同的 `version + dataPath + buildTimeUnixMs + allMatches` marker。相同 marker 的 verified current 也只能在共享 5 小时时效预算内复用；超龄、缺失或非法时间必须重新抓取，失败时保留 last-good 而不能伪报 `not_stale`。排行英雄 ID 必须属于固定 Catalog，所有 `all/stages` 海克斯 ID 必须属于 Catalog 正数唯一 `cdragon_id`；重复 ID、非法 rate、空 stage、概要错位、marker 漂移或任何未知 ID 都拒绝整个候选。逐英雄投影由 `scoped_stats/manifest.json` 逐文件记录 path、size、SHA-256 与 record count，消费者同时校验索引和子文件。

Blitz 排名使用 `data.v2.iesdev.com` 的公开 ARAM Mayhem JSON，作为 ARAMKit 当前 Stage 与同英雄 `all` 都缺失时的第三层回退。该来源单请求、无需登录，只允许静态 `fetch_text`，单响应上限 2 MiB；403/429 立即失败。Scrapling 是主路径，只有最终 TLS/network 故障且 circuit 与剩余总预算允许时才执行一次 `requests` 静态 fallback；403/429、schema、identity、coverage、invalid payload 和大小门失败都不 fallback，实际 backend/fallback provenance 写入 manifest/report。artifact 只保存 `patch`、数据日期、海克斯全局 tier 与最多五个英雄的专属 tier，marker 为 `patch + data_date + record_count + canonical content SHA-256`。它不提供胜率、选择率或样本量，generation 和 UI 都不得从 tier 推断百分比。候选要求所有身份和英雄均能绑定同代 Catalog，production pool 覆盖至少 95%；2026-08-15 的 16.16 合同为 423 条、覆盖 232/237，未覆盖 `1343/2108/2109/2126/2148`，这些条目保留识别能力并明确显示公开来源暂无排名。

生产识别闭集与统计来源解耦：eligibility、中文名、稀有度 tier、图标和视觉 variant 继续由同一 Catalog generation 的 `augment_assets.v1.json` 冻结；Blitz 与 ARAMKit 都不能增删识别候选。generation builder 逐 ID 比较 production pool 与 Catalog asset 的路径和 SHA-256，任一多出、重复或未绑定都在 snapshot 发布前失败并由 promotion journal 回滚；Stage、`all` 和 Blitz 都缺失才形成 `SOURCE_STAT_MISSING`。

ARAMKit marker 变化会把 Catalog、ARAMKit、Blitz、Apex、Mayhem 全部纳入同一 refresh cycle；Blitz 自身每 2 小时按内容 marker 检查。轻量 marker probe 失败只取消加速，不把网络瞬断升级为来源失败；正式抓取和 generation 门禁仍独立 fail closed。

Apex 由稳定 slug map 直接构造英雄详情 URL。提取层分别返回结构化结果和有限错误诊断，合法空结果必须继续进入页面分类；结果只能是 `has_synergy`、有页面身份和明确空态证据的 `confirmed_empty`，或带 `FailureKind` 的失败。解析异常和未知空结果不能发布，Apex/Mayhem 只通过同一 cohort 原子晋升。

Mayhem 优先解析 manifest JSON，HTML 仅为结构化 fallback。reject 带稳定原因码和有限样本；空结果、规模回退或 reject 比例越界都保留 last-good。

公共 transport 统一记录 URL、backend、状态码、耗时、尝试次数、失败分类和可重试性。静态 `get` 路径只加载 `Fetcher`；只有显式 browser 模式才加载 `DynamicFetcher` 与 browserforge。timeout、TLS、network 和 5xx 有限退避；403/429 按 host 熔断。不使用 stealth、验证码绕过、登录态或真实浏览器 profile。

来源 worker 的新 candidate 成功与 last-good 可用是两个状态：刷新失败时即使 fallback 可读，也必须返回 `success=false`，并携带 `reason_code`、`failure_stage`、`fallback_used`、`last_good_available` 和有限诊断。协调器按 `reason_code` 写入 schedule 的 `failure_kind`，保留旧 current，不把 fallback 记成本轮成功或混入新 generation。

正式活动 Catalog 与待采用 Catalog 分为两条通道。`refresh_checkpoint.v1.json` 只推进活动 Catalog：ARAMKit fresh 后可发布新 generation；Blitz 失败时仅复用同 Catalog verified last-good，标记 `last_good/data_stale/production_coverage_insufficient` 并从公开 DTO、session report 和 Canvas 清空 tier/rank；Apex/Mayhem 成对复用同 Catalog last-good。ARAMKit 失败不发布 generation。不同 Catalog 的旧 `full_catalog_rebind` checkpoint 会原子复制到 `catalog_adoption_checkpoint.v1.json` 并 blocked，原证据只标记 migrated；新 Catalog 只有 Blitz 覆盖至少 95% 且四来源全部重绑后才能整体晋升，禁止跨 Catalog pointer。历史 `hextech/stats` generation 仍可只读和回滚；新 generation 必须包含 `aramkit/scoped_stats`，Blitz stale 时允许以明确降级 provenance 存在但不得向用户展示排名。

活动 Catalog 下的 ARAMKit 与 Blitz 兼容投影仍保持闭集：未知 ID 只有在 blocked adoption Catalog 的文件/hash 已验证且明确包含该 ID 时，才可从活动 artifact 中过滤，并写入 `compatibility_filtered_augment_ids`。Blitz 过滤后重算 artifact marker；两条来源都不因此切换 Catalog、恢复 adoption lane 或扩大 production pool。其他未知海克斯、未知英雄、schema 错误、覆盖不足和错绑继续拒绝整轮。验收器允许这种可证明的 `adoption_held` Catalog 与 optional stale，但不放宽 ARAMKit freshness、artifact hash、完整 provenance 或同 Catalog 绑定。



---

- Desktop窗口呈现：`DesktopWindowPresentation`持有显隐/关闭/停靠状态，25ms Tk tick消费后台容量一快照。`champ_select_only`为默认，`client_right`仅作候选验收回退；两者禁止向左钳制覆盖客户端。本地LCU单在途观察在前台约250ms、后台1.5s运行，先发布阶段再更新列表。窗口操作、销毁和跟随恢复只在GUI线程执行；隐藏启动不阻塞后台就绪。

---

DataService 同时只运行一个 refresh cycle。`POST /v1/actions/refresh` 接受
`scope=due|core` 与 `force`；空 body 保持原有到期检查。Desktop 标题栏的“刷新”发送
`scope=core, force=true`，强制检查 ARAMKit/Blitz，同时继续处理正常到期的 Optional；
ARAMKit marker 变化或 Catalog 身份变化仍扩展为完整同代刷新。运行中重复触发合并为一次
`pending_recheck`，force 与覆盖范围按不丢失更强请求的规则合并；当前周期结束后立即重算。
shutdown 会拒绝新触发并清除 pending，不启动后续 worker。活动 `refresh_checkpoint.v1.json` 与 blocked `catalog_adoption_checkpoint.v1.json` 分离；两者的 `pending_sources` 都只能是 due 减 completed。活动 checkpoint 仅为 pending 来源保存有界的原因码、阶段、错误类型、fallback/last-good 状态和有限 diagnostics，剔除 traceback、命令行、环境、proxy 与凭据类字段；来源成功后对应失败证据消失。恢复时重新验证 Catalog ID/SHA、pointer、manifest 与 artifact SHA/size，只复用仍完整绑定的候选，半写或跨 Catalog 进度不进入活动发布通道。

存在可用 current generation 时，对局期间的自动刷新和手动核心刷新统一延后。DataService 的独立探针组合 Live Client 2999、LCU gameflow、游戏进程与窗口；接口 unknown 但进程/窗口存在时保守按在局中，Host visibility 只作补充诊断。活动 worker 每不超过 50 ms 检查 cancel signal，取消后给 2 秒协作退出，再关闭 Job Object 回收进程树；游戏取消、shutdown、hard timeout 分别归因，前两者不得写来源 backoff 或在 shutdown 后发布 generation。checkpoint 与延后门同时保留原始 `scope/force`，对局结束 30 秒后只恢复一次等价请求。`GET /v1/status.refresh_status` 统一暴露 state、scope、phase、reason、generation、pending 与起止时间，Desktop 将 running/deferred 持续显示，终态显示 6 秒；冷启动无 snapshot 时仍允许刷新。

Overlay 在有效 `session_id` 首次出现时固定整局 `stats_generation_id`；同局 current 更新只记录 `new_stats_generation_id`，下一局才采用。champion、Stage、ARAMKit run 和 immutable scoped view 仍按 `session_id + selection_epoch` 分轮固定。Stage 优先使用同一 Live Client 响应中的玩家等级（3–6/7–10/11–14/15+ 对应 Stage 1–4），等级缺失时才使用本局已确认选择数加一；每轮最多等待两秒后按现有信息冻结，短暂隐藏继续保留该轮范围，下一 epoch 只重算 Stage/scoped view，不更换整局 generation。



---

Snapshot manifest 保持 immutable。`DataSnapshotView.status(now=None)` 每次读取按共享来源周期的 `×1.25` 阈值实时投影 `data_status/data_reason/stale_age_seconds`，优先使用来源 `data_at`，旧代缺失时回退 `created_at`；自然变旧不改 `health`、`degraded_sources` 或 lineage `freshness`。`effective_degraded_sources` 合并发布时降级与消费者数据来源的读取时过期；Catalog 过期由独立 `adoption_held` 门处理，manifest 明确声明的 Catalog 降级仍会保留。Overlay 遇到 stale 必须清空胜率、出场率、ranking/tier 并显示数据年龄或“统计数据暂非最新”；严格 verifier 与 Desktop 优先消费 effective 字段。

---

verified bundle 是自包含 cohort：snapshot seed 与 Catalog、ARAMKit、Blitz、Apex、Mayhem 四来源 immutable run、production assets 一起进入包。冻结 composition root 在启动 Desktop 服务前先恢复 promotion journal，再对 current、previous、recovery point、本地 generations 与 bundle generation 重建完整 cohort，按 `created_at` 选择最新有效代；bundle 比运行态旧时不允许倒退，旧 current 被错误倒退时以 `runtime_restored` 恢复更新的本地代。选中 pointer、同代 schedule 与 recovery point 在同一 journal 中提交，snapshot pointer 始终最后提交；retention 保护 recovery point 引用。已有相同内容按 SHA-256 复用，旧 generation、报告和用户数据保留。部署窗口若完成到期刷新，部署验收只接受完整验证、单调更新且不改变 Catalog/production pool 的 generation，并分别核对 Sidecar Vision generation 与 Host stats generation；失败回滚覆盖 Catalog/四来源/snapshot current/previous、schedule、checkpoint、recovery point、selection、adoption checkpoint 与 promotion journal，避免只恢复 pointer 却留下跨代诊断状态。`--refresh-data` 构建路径以刷新后的运行态 snapshot 为唯一 seed 输入，packaged smoke 再用真实 Sidecar `--once` 验证生产池和矩阵绑定；packaged smoke 不是 League 真机验收。

---

Host 的 Tk 线程仅负责轻量事件/Context gate、窗口显隐与 Canvas 绘制；`OverlayDataPreparation` 单线程负责 verified snapshot、窄 display hint 索引、scoped stats 和 recommendation 投影，容量一请求队列按身份 coalesce。启动先后台预热 seed，不绑定未开始的游戏；本局 generation 在可信游戏实例出现后固定，统计和联动结果均携带请求版本，跨局、换英雄或碎片硬门使在途结果失效。原 DataSnapshotView 全量接口不变，Host 使用 `get_overlay_display_hints()` 避免复制全英雄统计。
