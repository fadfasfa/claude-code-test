# sm2-randomizer 标准数据更新流程

本文件是给 Claude Code / Codex 的 SOP：用户用自然语言要求"更新数据"时，按本流程执行。人工只负责把新 Excel 放进 `sm2-randomizer/`。

## 触发场景

用户说以下任一时，按本流程执行：
- "更新数据" / "跑一下数据更新" / "刷新数据"
- "我放了新 Excel" / "Excel 更新了，跑一下"
- "看看数据有没有变化"

## 前置确认

1. 确认 `pipeline/collect/excel/星际战士2数据表.xlsx` 是用户刚放的新版（看文件 mtime，或直接问用户"Excel 已放好？"）。
2. 在 `sm2-randomizer/` 目录下执行所有命令。

## 执行

跑标准流程入口（agent 无关，CC/CX 均可）：

```powershell
python scripts/sm2_update_flow.py --headless
```

参数选择：
- 默认（含 wiki 增量抓取）：用户没特别说，或游戏可能版本更新时。
- `--skip-wiki`：用户明确说"只改了 Excel" / "wiki 不用动"时。秒级完成。
- `--force-refresh`：怀疑 wiki revision/raw 不同步、或用户要强制全量重抓时。

默认流程会先从 Steam 公开 News API 选取最新的 `Patch Notes` / `Hotfix`，再批量查询 Fandom revision。revision 未变的职业/武器页不下载 HTML，本地天赋和图标也不刷新。

## 停点（重要）

流程**停在变动清单**，不会自动 apply、不会自动打包。执行后向用户呈现：
1. 终端摘要（issue_count / 版本对齐 / 变动数 / wiki 退化）。
2. 人审报告 `pipeline/store/reports/source/update_review.md` 的关键节（概览 + Excel 导入 + wiki 抓取 + 语义变更 + 结论）。

**不要擅自 apply 或 package。** 等用户拍板。

## 用户拍板后

- 用户说"覆盖"/"打包"/"发布"/"确认更新"：
  - 版本对齐且无硬退化（aligned=True, hard_degraded=False）：`python build_release.py apply-candidate` → `python build_release.py package-release [--with-exe]`
  - 版本不齐（aligned=False，用户确认要发）：`python build_release.py apply-candidate --accept-version-mismatch` → `python build_release.py package-release --accept-version-mismatch [--with-exe]`
  - wiki 硬退化（hard_degraded=True，用户确认要发）：apply/package 两步都必须显式带 `--accept-hard-degradation`；若同时版本不齐，同时带 `--accept-version-mismatch`。
- 用户说"先不改"/"算了"/"丢弃"：`python build_release.py clean-candidate`
- 用户说"wiki 退化要修"：排查 `pipeline/store/raw/wiki/原始抓取数据.json` 的 `meta.degradation`（reasons/soft_reasons），修 `pipeline/collect/wiki/scrape_wiki.py` 对应选择器。

## 退化处理

- `wiki_degraded=True`：软退化（字段选择器失效等），流程仍完成、issue_count 仍可能为 0；退化详情保留在内部报告，不混入给用户审查的前端数据变更清单。
- 校验 `issue_count>0`：硬退化或数据缺失，**不可 apply**，提示用户看 `pipeline/store/reports/runtime/runtime_validation.json`。

## 输出物

- `pipeline/store/reports/source/update_review.md`：前端数据变更清单（主看），只列职业、职业武器池、天赋、策略词条和策略规则的具体旧值/新值。
- `pipeline/store/reports/source/update_review.json`：上述前端数据变更清单的机器可读版本。
- `pipeline/store/reports/source/excel_import_report.json`：Excel 导入明细。
- `pipeline/tmp_publish/diff_summary.md`：候选 vs 当前 app/data 的逐字段 diff。
- `pipeline/store/reports/runtime/runtime_validation.json`：校验报告。

数组顺序变化不改变随机池成员，不进入前端人审清单；构建时间、抓取统计、文件路径和实现细节也不进入该清单。

## 不做的事

- 自动识别官方版本，但不自动 apply/package，也不绕过版本对齐和人审闸门。
- 不自动 apply/package（人工显式触发）。
- 不擅自修 wiki 选择器（退化时报告，由人工/后续任务修）。
