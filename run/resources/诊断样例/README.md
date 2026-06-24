# 诊断样例

本目录是离线诊断和回归样例的中文事实源。

当前事实源包括：

- `overlay_vision_fixtures/**`
- `overlay_matching_truth.v1.json`

维护约束：

- 诊断样例可用于离线自检和视觉匹配回归。
- 不放置真实运行日志、用户本机截图、浏览器 profile 或长期调试输出。
- 真实用户环境产生的诊断输出应继续落在 `data/runtime/debug/**`，并保持不入库。
