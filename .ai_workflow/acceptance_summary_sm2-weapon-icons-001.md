# Acceptance Summary

- 范围：从 `sm2-randomizer/星际战士2数据表.xlsx` 导入武器图片到 `sm2-randomizer/assets/weapons/icons/`
- 实现：新增 `sm2-randomizer/processing/import_weapon_icons.py`，补齐 `sm2-randomizer/data/import/武器图片映射.json`
- 结果：`sm2-randomizer/data/manifests/武器图标清单.json` 声明的 29 项武器图标已全部本地落盘
- 验证：导入脚本运行成功；最终 `existing_count = 29`，`missing_count = 0`
- 文档：已更新 `sm2-randomizer/PROJECT.md`
