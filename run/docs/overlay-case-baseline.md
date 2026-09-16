# Overlay 独立真值案例基线

## 范围与证据等级

本页固定历史问题、现有规则及可重复反例的对应关系，不把历史总结当作当前真机测量。2026-09-14 本轮只修改离线验收工具和确定性测试；不改生产识别、数据抓取、运行配置或部署入口。

| 案例 | 可核验原始来源 | 证据边界与当前验收 |
| --- | --- | --- |
| 游戏内刷新慢、部分海克斯未展示，用户怀疑数据链路 | [2026-07-06 原始用户反馈](C:/Users/apple/kb/07%20归档/AI对话归档/codex/2026-07/20260706-排查游戏日志与海克斯链路-7414effa.md#L40) | 原文是症状与假设，不足以断言抓取端根因；本轮不重演历史游戏。 |
| Overlay 反复修改牵动其他部分，希望复用 Web 数据但不常开 Web 前端 | [2026-07-14 原始用户问题，行 971](C:/Users/apple/kb/07%20归档/AI对话归档/codex/2026-07/20260714-整理-run-overlay-文档-73251c43.md#L971)；[用户批准计划，行 1183](C:/Users/apple/kb/07%20归档/AI对话归档/codex/2026-07/20260714-整理-run-overlay-文档-73251c43.md#L1183) | 当时提出同代数据、独立写入与完整展示链路；这些历史批准不构成本轮抓取、发布或架构重做授权。 |
| e4 约 7.82 秒无 READY，后续 button_hold 无新计算 | [r10 证据与范围、游戏内章节](overlay-r10-regression.md) | 当前可读的是文档记载的控制流证据；本轮未取得原始完整帧，不能构造人工名称真值或宣称原场景已修复。 |
| e8 碎片名称与右槽终极刷新冲突 | [r11 证据与实现边界](overlay-r11-deep-repair.md) | 内部 OCR/模板冲突，不是人工图像真值；文档所指 `.artifacts/r11-negative` 历史材料未作为本轮原帧输入。 |
| 自身 candidate/body_shard 把人工选择 span 早帧排除 | [r11 验收章节](overlay-r11-deep-repair.md)；`tests/test_overlay_truth_probe.py` | 当前确定性反例：人工 seq=1..2，首帧 captured_at=10、后帧=12，正确三槽耗时应为 2080ms，不是过滤后的 80ms。测试 PNG 是生成的 provenance fixture，不是 League 截图。 |

## 离线真值合同

- `truth_spans` 必须由 `label_source=human` 给定，且完整覆盖每槽；不从 OCR、模板或 scene 状态自动生成真值。
- 人工区间内所有 recognition 观察都进入覆盖检查，包括 candidate、inactive、body_shard、非 hextech 和未 captured 观察。区间 start/end 必须同时有正确身份的 timeline 观察与可验证完整图；中间每个 recognition 都必须有图。`observation_seq` 可包含心跳，因此不凭空要求整数区间内每个数字都是 recognition。
- 首次正确时钟保留区间内早期候选捕获起点；非 active/hextech 的 READY 不算成功，错误 READY 仍记错。candidate-only 产生未确认槽，不是零样本成功。
- 缺失图像、缺失边界、未捕获和无效结果不可形成通过的更短子集；身份、正 frame ID、时间、SHA-256、图像可解码、OCR 绑定、重复与乱序检查保持有效。
- 向后兼容：span 未给 `selection_type` 时仍为原有 hextech 正标签。人工明确指定 `selection_type=body_shard` 或 `non_hextech` 时，可省略 augment ID/name，READY 计为 false-positive；保留 epoch、slot、generation 与范围字段。负标签不生成三槽正确延迟或未确认正标签超时。仅有负例仍不能通过完整选择延迟门，需另有正选择样本。
- 该工具只验 timeline 中的识别结果，未测量最终 Host 像素可见性。`whole_real_game_go` 恒为 false；全量、原生、打包 smoke、实际两屏游戏与五局验收分别报告。

## 本轮验证

使用已有解释器 `C:/Users/apple/claudecode/run/.venv/Scripts/python.exe` 在隔离工作树 run 执行 `-m pytest -q tests/test_overlay_truth_probe.py`：32 passed（原有 21 项加 11 个参数化反例）。覆盖 2080ms、早帧/中间帧/末帧遗漏、缺 timeline、未捕获、candidate-only、self body_shard 错分类及明确人工负例。此结果不等于真实五局通过，也不追认历史 raw 图像不存在时的识别正确率。
