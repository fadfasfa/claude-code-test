# Verified Generation Seeds

本目录保存打包首启使用的完整 generation：

```text
current.v1.json
generations/<generation_id>/
```

`current.v1.json` 必须引用同目录下经过 schema、数量和 SHA-256 校验的 generation。
首次启动只把该 generation 播种到 `var/snapshots/`，最后原子切换 snapshot current。
这里不保存零散 CSV、联动片段或运行中新抓取的数据。
