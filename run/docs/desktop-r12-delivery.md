# r12 桌面独立窗口交付记录

日期：2026-09-10。工作仅限 `hextech-overlay-aramkit/run`。本轮完成桌面修复、自动/隔离原生验证、独立候选打包和用户授权的快捷方式替换；不宣称真实双屏或游戏内识别验收完成。

## 修复及证据

- 客户端查找从三处按标题首窗查找改为统一可信LeagueClientUx/RCLIENT主窗选择。排除隐藏、最小化、cloaked、子窗口、工具窗及微型辅助窗；歧义不任取。
- 面板改为原生owner=0的本进程TkTopLevel独立窗口，客户端只提供视觉跟随目标。前台条件置顶与布局缓存分离，保留非激活、限频纠正、区域裁剪和子控件重绘。
- 自有样式写入检查错误码及实际回读；无owner不再误报客户端失前台。呈现异常单列presentation_failed及hide_confirmed，并继续下一周期。
- 审查发现的半成功映射遗漏已修：内部可见标志为False但实际已映射时仍撤窗，关闭也检查实际映射。独立审查复核GO，仅限代码发现关闭。
- 维持原有外观、字号和游戏内r11修复，不更换UI框架、识别模型、统计来源或阈值。

## 修改文件

生产代码：`modules/vision/client_window.py`；`interfaces/desktop/{client_layer,foreground_layer,window_presentation,runtime_window,service_manager,app_view,presentation_smoke}.py`；`bootstrap/desktop.py`。

构建及验证：`tooling/build/package.py`、`tooling/acceptance/smoke_packaged_startup.py`；`tests/test_client_window_selection.py`、`test_desktop_runtime_overlay_windows.py`、`test_desktop_window_presentation.py`、`test_desktop_foreground_layer.py`、`test_desktop_client_layer.py`、`test_desktop_native_write_contract.py`、`test_desktop_presentation_smoke.py`、`test_desktop_r10_stability.py`、`test_packaged_desktop_presentation_smoke.py`、`test_package_deployment.py`。

文档：`docs/README.md`、`overlay-runtime.md`、`desktop-stable28.md`及本记录。以上路径均相对本run目录，生产代码相对`src/hextech`；全部既有脏改保留。

## 验证

- 全量：`.venv/Scripts/python.exe -m pytest -q -m 'not native'`，1945 passed、19 subtests。仓库没有native marker，该命令实际没有排除名称含native的测试；原生测试在进程内串行调度。
- 独立原生组：`test_desktop_client_layer.py test_desktop_foreground_layer.py test_desktop_presentation_smoke.py -k native`，13 passed。
- 后续呈现/隐藏/冻结门等增量组108 passed；最终选择器/关闭/写入合同等纯回归80 passed；调用层及构建组107 passed。新增诊断字段导致旧SimpleNamespace测试替身缺字段，已改用真实ClientWindowProbeResult并重跑通过，未放宽断言。
- 开发门：`pytest -q -m dev_gate`，81 passed。
- `ruff check src tests tooling`通过；项目Pyright 0 errors，保留sidecar_diagnostics.py动态`__all__`的一条既有warning；冻结烟测相关文件专项Pyright 0 errors/0 warnings；`git diff --check`无差异错误。
- Scrapling同步example.com smoke通过，未刷新生产数据。
- 第一次打包：冻结桌面和clean链路通过，但populated_runtime桌面进程收到完全退出信号、returncode=0，完整门未通过；信号来源未证实。失败现场保留在`.artifacts/s12`，不能删除或将首次失败改写为通过。
- 同一二进制在独立`.artifacts/s12b`仅复核一次，clean/stale_sidecar/populated_runtime三组全部通过。冻结桌面首显85.85/137.36/65.12ms，隐藏3.77/2.92/1.72ms；实际控件映射、不透明背景像素、owner=0、普通/置顶自有遮挡恢复、前台不变、资源回收均通过。
- 完整复核JSON：`.artifacts/r12/packaged-recheck-report.json`。上述合成前台和自有窗口数据不是实际League呈现或准确率数据。

## 候选与快捷方式

- 候选：`.artifacts/r12/releases/HextechCompanion-20260910-r12`，同目录名ZIP已生成。
- Build：`20260910T105442Z-b73b801c5734`；源码指纹已与当前生产源码重新比对一致。
- 使用已验证snapshot generation `20260910T080753-0c6296c957`，未触发远程刷新。
- 既有`C:/Users/apple/OneDrive/Desktop/Hextech伴生终端.lnk`经唯一`update_shortcut`辅助函数改指向r12；TargetPath、WorkingDirectory、Arguments（空）、IconLocation四字段均回读通过。
- 原快捷方式按字节备份到`.artifacts/r12/shortcut-before-r12.lnk`，SHA256=`18BEA651BFD28EF732E23894BD813A7285306628F6529D140DCFE319CD129673`；替换后SHA256=`B962CCB2BF54BC1514177C6128E97594447FB83038EADEA19BBCF060F8EA0128`。
- 正式`C:/HextechCompanion` EXE/manifest及`C:/HextechCompanion.previous` manifest哈希与修复前一致。未覆盖正式安装、未清理旧包；仅构建临时产物由打包器正常回收、新候选由staging整理到release。
- 无staging/commit/push；未修改另一份`claudecode/run`，未终止或接管用户正在运行的r11进程。

## 真机边界

现场只读确认LeagueClientUx PID8088仍运行，但主RCLIENT HWND330434最小化、客户区0×0、处于Windows最小化坐标；另9个同类窗口隐藏且136×39。当前missing代表没有可呈现主窗口，不是没有进程。同时实际游戏进程运行，桌面隐藏符合规则。

快捷方式替换不升级正在运行的r11。当前对局结束后应从旧程序托盘退出Hextech，再通过原快捷方式启动r12。真实客户端前台恢复/十次往返、两屏位置、五局识别速度与成功率尚未完成，不得以本次打包或内部READY代替。首次冻结链的退出信号来源仍未证实，复核通过不消除该记录。
