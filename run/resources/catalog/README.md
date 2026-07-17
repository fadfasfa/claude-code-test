# Catalog

本目录保存英雄、海克斯和版本的稳定闭集，是抓取范围、身份校验和 generation
构建的基础输入。

- `英雄目录.v1.json`：英雄 ID、名称、别名、详情和稳定 slug。
- `海克斯资源目录.v1.json`：海克斯身份、图标和 Apex slug。
- `hero_version.txt`：目录对应的英雄资料版本。

Web 通过 `/catalog/{filename}` 暴露必要的只读投影。运行态来源、缓存、日志、锁和
profile 不得写入本目录。
