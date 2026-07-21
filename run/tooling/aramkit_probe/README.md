# ARAMKit 独立抓取验证项目

## 功能简介

本目录用于开发期验证 ARAMKit 全英雄统计的可用性、完整性、耗时和 schema 稳定性。
它只在评估是否替换现有 ARAMGG 来源时运行，不属于 DataService，不发布正式快照，
也不修改任何 current pointer、seed、资源 manifest 或打包入口。

工具只请求 `data/versions.json`、指定 dataset 的 `champion-rankings.json` 和逐英雄
`champion-details/<id>.json`。不会请求 `resourcePath`，因此不抓英雄或海克斯名称、
描述、图标和图片。

## 使用方法

在 `run/` 目录使用项目虚拟环境运行：

```powershell
.\.venv\Scripts\python.exe -m tooling.aramkit_probe fetch
.\.venv\Scripts\python.exe -m tooling.aramkit_probe fetch --dataset high
.\.venv\Scripts\python.exe -m tooling.aramkit_probe fetch --version 16.14
.\.venv\Scripts\python.exe -m tooling.aramkit_probe compare --dataset all --latest 2
```

- 默认 dataset 是 `all`。
- `high` 与 `all` 是二选一运行，不会在一次 fetch 中同时下载。
- 默认并发和最大并发均为 8；可用 `--concurrency 1..8` 主动降低。
- `all` 已经包含全阶段和阶段 1–4 海克斯，不需要为了阶段数据抓 `high`。
- ARAMKit 没有公开 `high` 的精确筛选规则，不能把它视为已证实的具体段位或 MMR 阈值。

## 产物

运行产物写入已被 Git 忽略的目录：

```text
run/var/aramkit_probe/
  runs/<UTC-run-id>-<dataset>/
    manifest.json
    snapshot.json.gz
    raw/champion-details/<id>.json.gz
  comparisons/<UTC-time>-<dataset>.json
```

- `manifest.json` 记录来源版本、并发、耗时、流量、延迟、覆盖计数、错误和产物哈希。
- `snapshot.json.gz` 只包含未来替换 ARAMGG 所需的英雄与阶段海克斯统计。
- `raw/` 保留源站原始详情，供 schema 变化时复核；保存原文不会增加网络请求。
- 失败 run 也保留 manifest，但 `complete=false`，不得作为替换依据。

## 完整性门禁

一次 run 只有同时满足以下条件才会通过：

1. 排行英雄集合与详情英雄集合完全一致。
2. 详情英雄 ID 和排行概要完全一致，避免不同版本数据混用。
3. 每个英雄都有 `augments.all` 和阶段 `1`、`2`、`3`、`4`。
4. 同一英雄、同一范围没有重复海克斯 ID。
5. 比例字段位于 `[0,1]`，rank 为正整数，sampleCount 非负。
6. 所有英雄完成后，原始文件和标准化快照均写入成功并记录 SHA-256。

网络失败只对 timeout、连接错误和 5xx 做一次并发 2 的尾部重试。遇到 403 或 429
会立即停止继续调度，避免对第三方基础设施造成额外负担。

## 替换 ARAMGG 前的决策清单

至少保留两次独立的 `all` 全量 run，并执行 `compare`。替换前确认：

- 两次 manifest 都是 `complete=true`，英雄和阶段覆盖完整。
- 同一 `dataPath` 的两次内容指纹完全一致；不同 `dataPath` 的变化英雄有明确记录。
- P50、P95、最大延迟和总耗时符合可接受范围，且没有 403/429。
- `all` 的字段语义能映射现有 ARAMGG 契约，尤其注意英雄 pickRate 的总体口径不同。
- 正式接入时另行设计发布、回滚和 ARAMGG fallback；本工具本身不得直接升级为发布入口。
