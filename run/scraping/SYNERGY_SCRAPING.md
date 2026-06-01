# ApexLoL synergy 抓取收尾说明

本文记录 ApexLoL 英雄联动抓取端的现行后端、发布语义和自动刷新边界，供人工手动维护 synergy 数据时读取。

## 后端

- `scrapling get` / requests 路径：优先读取本地 snapshot；允许在线抓取时先走普通 HTTP 页面和资源发现。
- `CloakBrowser` 路径：通过 `APEX_ALLOW_CLOAKBROWSER=1` 启用，可穿过部分 Cloudflare 挑战并取得最终 HTML。
- 已知限制：CloakBrowser 不能稳定解决 ApexLoL 的 IP 级反爬；全量抓取中仍可能出现 `access_denied`、Next.js 5xx 或缺少联动 hydration 的非 origin 页面。

## 手动命令

在 `run/` 目录执行，确保 `data/static/Champion_Core_Data.json` 已就位。

单英雄探针：

```powershell
$env:APEX_ALLOW_ONLINE_FETCH="1"
$env:APEX_ALLOW_CLOAKBROWSER="1"
python -m scraping.full_synergy_scraper --single-champion Vi
```

全量验证，不发布 latest：

```powershell
$env:APEX_ALLOW_ONLINE_FETCH="1"
$env:APEX_ALLOW_CLOAKBROWSER="1"
$env:APEX_VALIDATE_FETCH_ATTEMPTS="3"
$env:APEX_VALIDATE_DELAY_SECONDS="2"
python -m scraping.full_synergy_scraper --validate-full --delay-seconds 2
```

全量发布：

```powershell
$env:APEX_ALLOW_ONLINE_FETCH="1"
$env:APEX_ALLOW_CLOAKBROWSER="1"
$env:APEX_VALIDATE_FETCH_ATTEMPTS="3"
$env:APEX_CLOAKBROWSER_TIMEOUT_MS="45000"
Remove-Item Env:\APEX_DRY_RUN -ErrorAction SilentlyContinue
python -m scraping.full_synergy_scraper
```

## 发布 merge 语义

`main()` 构建新 payload 后、写入 `SynergyWriter.write()` 前，会读取 `Champion_Synergy_latest.v1.json` 指向的旧 latest 快照并逐英雄合并：

- 新 payload 中有联动条目的英雄使用新数据。
- 新 payload 中空、缺失或本轮因 blocked 导致无有效结果的英雄，保留旧 latest 中该英雄的非空联动数据。
- 旧 latest 不可读或结构不匹配时，按全新 payload 发布，并在日志和返回结果的 `merge.reason` 中标注。
- merge 后重新运行发布规模熔断，避免正式数据净回退。

## 自动更新状态

synergy 自动更新已代码级硬关，仅支持手动触发。原因是 ApexLoL IP 级反爬导致全量自动抓取不可靠；即使设置 `HEXTECH_AUTO_SYNERGY_REFRESH=1`，`orchestrator` 与 `heal_worker` 也不会自动刷新 synergy。该变更不影响 hextech 排名和 augment 图标刷新。

## 新版本恢复依据

恢复自动更新前至少需要重新验证：

- ApexLoL 新版本页面是否仍有稳定的英雄详情 URL、联动 marker 和 hydration 结构。
- 单英雄与全量验证中 `access_denied`、origin 5xx、缺联动 hydration 的比例是否降到可自动发布。
- `failed > 0` 时仍必须 `publishable=false`；正式发布依赖 merge 兜底，不能把本轮 blocked 英雄覆盖为空。
- 若页面结构改变，只微调 marker、origin 校验或 extractor 映射；不要为了绕过反爬扩大自动调度面。
