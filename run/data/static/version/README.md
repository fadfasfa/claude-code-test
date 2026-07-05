# static/version

本目录是版本级稳定 JSON / TXT 数据事实源。

旧 `data/static/` 与 `data/indexes/` 的稳定数据已合并到这里。旧 Web 路由
`/data/static/...` 与 `/data/indexes/...` 仍作为兼容入口挂载到本目录。

当前权威目录文件：

- `英雄目录.v1.json`：合并英雄别名、alias-to-id、id-to-name 与 id-to-detail。
- `海克斯资源目录.v1.json`：合并海克斯 manifest、name-to-icon 与 apexlol slug map。
- `Champion_Synergy_Cleaned.json`：前端协同数据。
- `hero_version.txt`：当前英雄资料版本。

维护约束：

- 这里放可随版本变化、可随包分发的稳定数据。
- 不把 `data/runtime/raw/**` 或迁移前旧 `data/raw/**` 抓取快照当作普通版本数据。
- 不把 `data/runtime/**` 缓存、日志、锁、profile 或本机状态放入版本数据。
- 旧拆分文件名只作为 Web/API 兼容投影，不再作为源码态事实源。
