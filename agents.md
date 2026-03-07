## 任务状态机契约 (agents.md)

分配节点：Node A (Claude Sonnet 4.6)
路由决策依据：基建层规约变更，需要精确修改三个核心防线文件，优先语义理解与精准改写能力。
全局上下文状态：意图已锁定待执行

---

### 前置条件 (Pre-conditions)
- HMAC 契约签名：待核验（当前处于 Bypass 模式）
- DACL 状态：需先执行 Unlock-Core.ps1 解锁后方可操作
- pre_merge_snapshot.txt：待生成
- STASH_CREATED 标记（路径：`.ai_workflow/STASH_CREATED`）：false

### 后置条件 (Post-conditions)
- `post_check_diff.py` 的导入检查逻辑能放行全量 Python 标准库模块，不再因标准库新增导入触发 ERR_AST_VERIFY_FAILED。
- `Lock-Core.ps1` 仅保护根目录四个核心文件与 `.ai_workflow/`、`.git/hooks`，不再引用不存在的 `scripts` 目录。
- `Unlock-Core.ps1` 与 Lock-Core.ps1 完全对称，覆盖相同的保护目标。
- 三个文件通过 [INFRA-BYPASS] 提交入库，DACL 重新上锁。

---

### 确定性执行清单

- [ ] Step 0: 解锁基建防线
  - 目标文件：无（执行脚本）
  - 结构化指令：在 PowerShell 中执行 `.\Unlock-Core.ps1`，确认输出无报错后方可继续。
  - 风险提示：解锁期间禁止执行任何无关的 git 操作。

- [ ] Step 1: 扩展 post_check_diff.py 导入白名单
  - 目标文件：`.ai_workflow/post_check_diff.py`
  - 结构化指令：将 `check_imports` 函数中的 `ALLOWED_NEW_IMPORTS` 集合及判断逻辑替换为基于标准库模块名前缀的动态放行机制。具体实现：定义 `STDLIB_MODULES` 集合，收录全量 Python 标准库模块名（见下方清单）；在判断新增导入时，先解析 key 中的模块名（`import:X` 取 X，`from:X:Y` 取 X 的根模块名，即第一个点号前的部分），若根模块名在 `STDLIB_MODULES` 中则直接放行，不再硬编码逐条枚举。`STDLIB_MODULES` 至少包含以下模块：`abc, ast, asyncio, base64, builtins, collections, concurrent, contextlib, copy, csv, dataclasses, datetime, decimal, difflib, email, enum, fileinput, fnmatch, fractions, ftplib, functools, gc, getpass, glob, gzip, hashlib, heapq, hmac, html, http, imaplib, importlib, inspect, io, itertools, json, keyword, linecache, locale, logging, math, mimetypes, multiprocessing, numbers, operator, os, pathlib, pickle, platform, pprint, queue, random, re, shlex, shutil, signal, smtplib, socket, sqlite3, ssl, stat, statistics, string, struct, subprocess, sys, tempfile, textwrap, threading, time, timeit, tkinter, traceback, types, typing, unittest, urllib, uuid, warnings, weakref, xml, xmlrpc, zipfile, zipimport, zlib`。同时保留原有 `ALLOWED_REMOVED_IMPORTS` 逻辑不变。
  - 风险提示：仅放行标准库，第三方库（如 flask、requests、numpy）不在放行范围内，需单独加入显式白名单。解析根模块名时注意相对导入（以 `.` 开头的 key），相对导入一律放行。

- [ ] Step 2: 更新 Lock-Core.ps1
  - 目标文件：`Lock-Core.ps1`
  - 结构化指令：完整覆写为以下内容，精准锁定根目录四个核心文件与两个核心目录，删除已不存在的 `scripts` 目录引用：
```powershell
    Write-Host "Applying High Integrity physical lock..." -ForegroundColor Cyan

    # 锁定核心基建目录（向下继承，全面锁死）
    icacls ".ai_workflow" /setintegritylevel "(OI)(CI)H" /q | Out-Null
    icacls ".git\hooks"   /setintegritylevel "(OI)(CI)H" /q | Out-Null

    # 锁定根目录核心文件（单文件精准锁定）
    icacls "agents.md"       /setintegritylevel H /q | Out-Null
    icacls "Lock-Core.ps1"   /setintegritylevel H /q | Out-Null
    icacls "Unlock-Core.ps1" /setintegritylevel H /q | Out-Null
    icacls "run_task.ps1"    /setintegritylevel H /q | Out-Null

    Write-Host "Core locked! Protected: .ai_workflow/, .git/hooks/, agents.md, Lock-Core.ps1, Unlock-Core.ps1, run_task.ps1" -ForegroundColor Green
    Write-Host "Workspace folders (heybox/, QuantProject/, run/, .vscode/ and any new dirs) remain fully writable." -ForegroundColor Yellow
```
  - 风险提示：根目录下业务文件夹不在锁定范围内，后续新增目录默认为工作区，无需修改此脚本。

- [ ] Step 3: 更新 Unlock-Core.ps1
  - 目标文件：`Unlock-Core.ps1`
  - 结构化指令：完整覆写为以下内容，与 Lock-Core.ps1 完全对称：
```powershell
    Write-Host "Removing High Integrity physical lock..." -ForegroundColor Cyan

    # 解锁核心基建目录
    icacls ".ai_workflow" /setintegritylevel "(OI)(CI)M" /q | Out-Null
    icacls ".git\hooks"   /setintegritylevel "(OI)(CI)M" /q | Out-Null

    # 解锁根目录核心文件
    icacls "agents.md"       /setintegritylevel M /q | Out-Null
    icacls "Lock-Core.ps1"   /setintegritylevel M /q | Out-Null
    icacls "Unlock-Core.ps1" /setintegritylevel M /q | Out-Null
    icacls "run_task.ps1"    /setintegritylevel M /q | Out-Null

    Write-Host "Core unlocked! God-mode active. Remember to re-lock after infra changes." -ForegroundColor Red
```
  - 风险提示：解锁后必须在完成基建修改并提交后立即执行 Lock-Core.ps1，严禁长时间保持解锁状态。

- [ ] Step 4: 破冰提交
  - 目标文件：`.ai_workflow/post_check_diff.py`、`Lock-Core.ps1`、`Unlock-Core.ps1`
  - 结构化指令：执行精准暂存与破冰提交，随后重新上锁：
```powershell
    git add .ai_workflow/post_check_diff.py Lock-Core.ps1 Unlock-Core.ps1
    git commit --no-verify -m "[INFRA-BYPASS] 扩展标准库白名单至全量 stdlib；精准化 DACL 保护范围至根目录核心文件"
    .\Lock-Core.ps1
```
  - 风险提示：git add 严禁全量，只加以上三个文件。提交完成后检查 Lock-Core.ps1 输出确认上锁成功。

---

### Node C 终审记录（由 Node C 填写）
- 审查结论：[Node C 审计通过]
- 当前修复重试次数：[0/5]
- 风险清单摘要：[INFO] 基建变更，三个目标文件已符合契约要求，无需变更。
- 熔断原因（如有）：无

---

### 冲突避免与底层防线

- 语言规约：执行节点必须唯一使用简体中文（除代码块外）进行终端交互与日志输出。
- 权限隔离：Step 4 的 git add 严禁全量，必须精确指定三个目标文件。
- 破冰规约：本契约整体属于基建变更，Step 0 至 Step 4 全程处于破冰模式，Step 4 末尾必须执行 Lock-Core.ps1 重新上锁。
- 状态机闭环：本契约无暂存压栈需求，STASH_CREATED 保持 false，Node C 跳过 git stash pop。
