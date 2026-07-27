# SMS 验证码多来源监控

实时轮询 LuDan SMS 接码平台、kkdos 动态号、固定文本接码链接和邮箱取件接口，终端按来源显示号码/邮箱与最新验证码，并可在同一面板展示本机账户档案。

## 使用

1. 首次使用：复制 `config.example.json` 为 `config.json`，按需配置 LuDan `key`、`kkdos_sources` 的 CDK，或把 `fixed_sources` 里的 `url` 改成真实接码链接。
2. 双击 `run.bat` 启动监控；账户导入直接交给 CC/Codex 完成。
3. 面板会显示 LuDan / kkdos 动态号码、未归属账户的固定号码/邮箱来源和账户档案；已被账户关联的接码来源会聚合到账户卡片内，不在顶层重复列出。美国号码会拆成国家码 `+1` 与 10 位号码，复制区域只放 10 位号码。
4. 按来源序号手动复制对应 10 位号码/邮箱；检测到新的手机或邮箱验证码时会自动复制验证码。账户档案按序号后会进入复制子菜单，可复制登录邮箱、当前 2FA 动态码和 10 位手机号码；无 TOTP 密钥或密钥无效时不显示动态码复制项。
5. 启动后默认低频刷新，方便滚动查看历史；复制号码/邮箱/账户项或手动换号后，会进入高频轮询，拿到新验证码并自动复制后回到低频。多个接码来源会并发轮询，单个慢接口只显示本轮超时提示，不会拖住整轮刷新。

## 标准录入流程

给 Codex / Claude Code 执行录入时，优先使用 `config` 子命令，不直接手改 `config.json`。

1. 结构检查：`python monitor.py config validate --json`
2. 初始化或更新全局配置：`python monitor.py config init --json`，再用 `python monitor.py config set-global --key-env SMS_MONITOR_KEY --json`
3. 录入固定短信来源：`python monitor.py config upsert-fixed --label YunTL --phone 15550123456 --url-env FIXED_URL --json`
4. 录入 kkdos 动态来源：`python monitor.py config upsert-kkdos --label kkdos --cdk-env KKDOS_CDK --json`
5. 录入邮箱来源：`python monitor.py config upsert-email --label iCloud --email example@icloud.com --provider icloud --base-url https://email.nloop.cc --json`
6. 录入账户档案：`python monitor.py config upsert-account --label ChatGPT --login-email your-gmail@example.com --password-env ACCOUNT_PASSWORD --totp-secret-env ACCOUNT_TOTP --phone 15550123456 --phone-source-label kkdos --email example@icloud.com --json`
7. 预备接码检查：`python monitor.py config ready-check --all --json`

手机号可按原始格式录入用于来源匹配，但登录输入或复制时只使用美国 10 位本地号码，不要把 `+1` 等国家码计入目标输入框。

`ready-check` 的 `ready=true` 表示来源已经可等待验证码，不表示已经收到验证码。命令输出只包含来源 label、类型、状态和脱敏原因；真实 key、URL token、密码和 TOTP 密钥必须通过环境变量传入，不应出现在命令输出或对话里。

### Agent 双通道导入

#### 通道 A：普通或废弃账户

用户把资料直接粘贴到 CC/Codex 会话，并明确说：

```text
这是不重要账户，直接导入：<账户资料>
```

agent 负责解析自然语言、直接执行确认写入、运行 `validate` 和 `ready-check --all`，最后只报告脱敏结果；用户不需要操作剪贴板、菜单或命令。解析器支持单行、多行和多账户内容，以及 `ChatGPT谷歌邮箱`、`ChatGPT密码`、`一次性安全码密钥`、`二验手机号`、`验证码获取链接` 等字段；未提供名称时使用邮箱 `@` 前部分。因为原文进入了会话，这类资料不能再视为秘密。

#### 通道 B：重要账户

用户只需告诉 agent：

```text
准备 2 个重要账户导入位置。
```

agent 执行 `python monitor.py config private-template --count 2 --open`，创建并打开 `private-import.txt`。用户在文件中逐项填写 `名称 / 邮箱 / 密码 / 2FA / 手机号 / 接码链接`，填完后回到会话说：

```text
重要账户填好了，执行导入。
```

agent 随后直接调用 `import-private --yes --json`、`validate --json` 和 `ready-check --all --json`，只读取这些命令的脱敏结果。agent **不得**对 `private-import.txt` 执行 `Get-Content`、`cat`、`rg`、diff、复制、摘要或任何内容读取。导入成功后程序自动把文件恢复为相同数量的空白槽位；失败时保留原文供用户修正。

`private-import.txt` 及原子写入临时文件已被 Git 忽略。它在导入前仍是本机明文暂存区，不是凭据保险库。

### 无效来源与账户管理

LuDan 卡密失效（CDK 校验失败 403）或某固定/邮箱来源连续硬失败（HTTP 401/403/404/410 或网络异常，连续 5 次）时，监控会自动把该项标记为无效并持久化到 `config.json` 的 `disabled` 字典，跳过轮询但保留展示，不再因单个失效来源退出整个程序。429 / 5xx / 超时 / 暂无短信等临时状态不会触发禁用。面板底部会列出当前已禁用项及原因。

手动管理无效项（`--kind` 取值 `ludan` / `fixed` / `kkdos` / `email` / `account`，`ludan` 的 `--label` 可传任意占位值）：

```powershell
python monitor.py config list-disabled --json                                # 列出所有无效项
python monitor.py config disable --label eSIM88 --kind fixed --reason "链接失效" --json
python monitor.py config enable  --label eSIM88 --kind fixed --json          # 恢复轮询
python monitor.py config prune  --json                                        # 预览将清理的无效项
python monitor.py config prune  --yes --json                                  # 从 config 物理删除无效的固定/邮箱/账户项
```

`prune --yes` 会删除 `disabled` 标记的固定/kkdos/邮箱/账户项并清空 `disabled` 字典；LuDan 是顶层配置无法物理删除，`prune` 后其失效标记一并清除，下次启动会重新校验。定期用 `prune --yes` 清理无效账号即可。

### 自由文本导入

敏感来源文本不要贴进对话，使用通道 B。普通或废弃账户可以先复制到本机剪贴板，然后运行：

```powershell
python monitor.py config import-freeform --from-clipboard --label ChatGPT-1 --interactive --json
```

也可以在本机终端粘贴到标准输入：

```powershell
python monitor.py config import-freeform --stdin --label ChatGPT-1 --interactive --json
```

脚本会在本机内存中解析 `名称 / 账号 / 密码 / 2FA / 手机号 / 接码 URL`，支持中文或英文字段名、紧凑单行，以及用空行或分隔线隔开的多组账户。首次调用只返回整批脱敏预览、缺失字段、重名冲突和预计变更，不写文件；确认预览正确后再加 `--yes` 原子写入。任一组不完整或有冲突时整批保持不变：

```powershell
python monitor.py config import-freeform --from-clipboard --label ChatGPT-1 --interactive --yes --json
python monitor.py config ready-check --all --json
```

多组资料示例（真实值只放本机剪贴板，不贴入 CC/Codex 对话）：

```text
名称：ChatGPT-1
邮箱：user1@example.com
密码：...
2FA：...
手机号：+15550123456
接码链接：https://example.invalid/token-1

名称：ChatGPT-2
邮箱：user2@example.com
密码：...
2FA：...
手机号：+15550987654
接码链接：https://example.invalid/token-2
```

同一 `label` 会在预览确认后更新；相同登录邮箱属于其他 `label` 时整批拒绝。每个账户会同时创建或更新 `<label>-SMS` 固定来源，并通过 `phone_source_label` 显式关联。

## 用 Claude Code 导入

本节同时适用于 Claude Code（CC）和 Codex。普通/废弃账户可由 agent 调用 `import-freeform`；重要账户必须改用 `private-template` + `import-private`，agent 不得读取私密模板。需要精确更新单个字段时，仍可使用后面的结构化 `upsert-*` 命令和环境变量。

> 以下命令在 `sms-monitor/` 目录下运行；若从仓库根目录运行，等价写法为 `python sms-monitor/monitor.py config ... --config sms-monitor/config.json`。

### 命名约定

- 账户一体：账户 `label` = 邮箱本地名（@ 前部分，小写）；接码来源 `label` = `<本地名>-SMS`。两者通过**同一手机号**自动关联（`_link_accounts` 按 `split_us_phone(phone).raw_digits` 比对）。
- 单独电话：来源 `label` 用平台名（如 `YunTL`、`eSIM88`）或用户指定；无账户档案。
- `label` 幂等：同 `label` 重复导入为更新，不新增。

### 场景 A：单独电话导入

- 适用：只有 手机号 + 接码 URL，无邮箱 / 密码 / 2FA。
- 覆盖范围：仅支持**固定 URL 直接出码**（GET 一次返回验证码）。多步换号取码（需先输入字符换号、再查码，类似 LuDan）暂不覆盖，见「注意事项」。
- 用户贴入格式（示例，CC 可理解变体）：

```
电话：14243554247
接码：https://app.yuntl.cc/apisms/<token>
```

  或单行：`14243554247|https://app.yuntl.cc/apisms/<token>`

- CC 执行（PowerShell，token 走环境变量）：

```powershell
$env:SMS_URL='https://app.yuntl.cc/apisms/<token>'
python monitor.py config upsert-fixed --label <平台名> --phone 14243554247 --url-env SMS_URL --json
```

### 场景 B：账号电话一体导入

- 适用：邮箱 + 密码 + 2FA + 手机号 + 接码 URL。
- 用户贴入格式（示例）：

```
邮箱：necocheadebbra@gmail.com
密码：YK85J7nv1b%TSkWI
2fa：WNZDDWJZUPD4T6XEODAAHE4MK46HWEQ2
14243554247|https://app.yuntl.cc/apisms/<token>
```

- CC 执行（两条命令，同一手机号关联；密码 / TOTP / URL token 全走环境变量）：

```powershell
$env:ACC_URL='https://app.yuntl.cc/apisms/<token>'
$env:ACC_PASSWORD='YK85J7nv1b%TSkWI'
$env:ACC_TOTP='WNZDDWJZUPD4T6XEODAAHE4MK46HWEQ2'
python monitor.py config upsert-fixed   --label necocheadebbra-SMS --phone 14243554247 --url-env ACC_URL --json
python monitor.py config upsert-account --label necocheadebbra --login-email necocheadebbra@gmail.com --password-env ACC_PASSWORD --totp-secret-env ACC_TOTP --phone 14243554247 --json
```

- 说明：`label` `necocheadebbra` 取自邮箱本地名；`-SMS` 来源与账户通过同一 `--phone` 自动关联；先建来源再建账户。

### 场景 C：kkdos 动态号导入

- 适用：kkdos 卡密/CDK 动态分配手机号，复制号码到 OpenAI 后再点击查询等待验证码。
- 行为：启动时只 `verify` 取号；复制 kkdos 号码后才调用查询接口并通过 SSE 等码，空闲刷新不会自动触发查询。`bindable` 且已锁定的卡密不会自动换号。
- CC 执行（CDK 走环境变量）：

```powershell
$env:KKDOS_CDK='YOUR_KKDOS_CDK'
python monitor.py config upsert-kkdos --label kkdos --cdk-env KKDOS_CDK --json
python monitor.py config upsert-account --label sk7398965 --login-email sk7398965@example.com --phone-source-label kkdos --json
```

- 说明：`phone_source_label` 优先于静态手机号匹配，适合 kkdos 这种动态号码；旧固定来源账户仍可继续只用 `--phone` 自动关联。

### 场景 D：msg-nest 动态号导入

- 适用：msg-nest 卡密/CDK 动态分配手机号，redeem 换 claimToken 后轮询取码（类似 LuDan，但 token 走 `x-claim-token` 请求头、会过期自动重取）。
- 行为：启动时 `POST /api/public/cdks/redeem` 换 claimToken 并取号；`claim_token`/`alloc_id`/`fingerprint`/`phone` 持久化到 `config.json`，过期或缺失自动重新兑换。每轮 `GET /api/public/allocations/{id}/messages` 轮询验证码，无需手动触发。
- CC 执行（CDK 走环境变量；已知 allocId 与号码可一并种子化，redeem 后核对尾号）：

```powershell
$env:MSGNEST_CDK='YOUR_MSGNEST_CDK'
python monitor.py config upsert-msgnest --label msgnest --cdk-env MSGNEST_CDK --alloc-id alloc_xxx --phone 15550123456 --json
python monitor.py config upsert-account --label someuser --login-email someuser@example.com --phone-source-label msgnest --json
```

- 说明：`alloc_id`/`phone` 为可选种子，不传则首次 redeem 写回；redeem 后号码尾号与种子不符会告警但不阻断。`claim_token`/`fingerprint` 由工具自管，不要手填；重录 CDK 时这两个字段会被保留，不会丢失已兑换状态。

### 多账号

- **逐个串行执行，不要并行**：多账号改同一 `config.json`，read-modify-write 非原子，并行会互相覆盖。
- 每个账号用独立环境变量名（如 `ACC1_*` / `ACC2_*`）避免串值。

### 导入后校验

```powershell
python monitor.py config validate    --json
python monitor.py config ready-check  --all --json
```

- `validate`：本地结构校验。
- `ready-check --all`：LuDan + 全部固定 / kkdos / 邮箱来源 + 账户关联；`ready=true` 即就绪；新接码链接应返回 `ready`（如「暂无短信」= 链接可达、暂无验证码）。
- 两者输出均脱敏，不含明文 secret。

### 注意事项

- 不手改 `config.json`：一律走 `config` 子命令（见上文「标准录入流程」+ `AGENTS.md` 凭据保护）。
- `private-import.txt` 只允许用户本人编辑；CC/Codex 禁止对它执行 `Get-Content`、`cat`、`rg`、diff 或任何内容读取。
- LuDan 卡密失效（403）不再退出整个程序：自动标记无效、跳过轮询、继续展示固定来源和账户；换 CDK 后用 `config enable --kind ludan --label LuDan` 恢复（已禁用项不会重新校验，需先 enable 才会再校验）。
- 敏感值不进命令行明文、不回显：用 `--*-env` 传环境变量；命令输出只有脱敏预览。
- `import-freeform` 已支持中文多行和多账户批量导入；结构化 `upsert-*` 命令继续作为自动化和精确更新入口。
- 手机号原样录入用于关联；明确 `+1` 或 10 位北美号码会拆出 10 位本地号。无 `+` 的其他 11 位号码不再默认当作美国号码。
- 取码方式覆盖范围：当前支持①固定 URL 直接出码（`fixed_sources`）、②LuDan 动态号（`LuDanSource`）、③kkdos 动态号（`kkdos_sources`，verify 取号、复制后 start/SSE 等码、允许时手动换号）、④msg-nest 动态号（`msgnest_sources`，redeem CDK 换 claimToken 取号、轮询 messages 取码、token 过期自动 re-redeem）。其他多步取码平台需先确认真实 API 后再扩展。

## 依赖

- Python 3 + `requests`（`python -m pip install requests`）。
- 剪贴板与热键功能依赖 Windows（Win32 剪贴板 API，`clip` 作为 fallback，`msvcrt` 负责热键）；其他系统仅 Ctrl+C 退出可用。

## 热键

- 输入完整数字序号可选择任意账户或来源（包括超过 9 项）；账户来源会先打开复制子菜单，菜单只列登录邮箱、有效 2FA 动态码和手机号码，`q` 或 `Esc` 取消
- `n` 对当前启用的可换号动态来源执行换号；kkdos 锁号或冷却时会显示服务端限制
- `q` / Ctrl+C 退出

## 配置项

| 字段 | 说明 |
| --- | --- |
| `base_url` | 开放 API 地址 |
| `key` | 你的 CDK（接口认证 key） |
| `poll_interval` | 高频轮询间隔秒数（默认 5，程序下限 2 秒）；45 次/60 秒属于服务端限频，程序收到 429 后被动退避 |
| `idle_poll_interval` | 低频轮询间隔秒数（默认 15）；未复制前按此节奏刷新，避免终端滚轮被频繁清屏打断 |
| `request_timeout` | 单个 HTTP 请求超时秒数（默认 3）；LuDan、固定链接和邮箱取件共用 |
| `max_poll_workers` | 并发轮询 worker 数（默认 4）；来源多时避免串行等待 |
| `poll_round_timeout` | 单轮并发轮询总等待预算秒数（默认 `request_timeout + 0.5`）；超时来源保留旧状态并显示提示 |
| `pending_poll_max_rounds` | 迟到 worker 最多保留轮数（默认 3）；结算只检查已完成任务，不额外阻塞下一轮 |
| `active_until_code` | 复制或手动换号后是否一直高频轮询到拿到新验证码（默认 `true`）；拿到验证码后自动回低频 |
| `active_after_copy_seconds` | `active_until_code=false` 时的高频轮询秒数（默认 180） |
| `auto_change_on_expire` | 号码过期时是否自动换号 |
| `fixed_sources` | 固定文本接码链接数组；每项包含 `label`、`phone`、`url` |
| `kkdos_sources` | kkdos 动态号来源数组；每项含 `label`、`cdk`、`base_url`（默认 `https://sms.kkdos.store`）。启动时 verify 取号，复制号码后 start/SSE 等码 |
| `msgnest_sources` | msg-nest 动态号来源数组；每项含 `label`、`cdk`、`base_url`（默认 `https://msg-nest.com`），`alloc_id`/`claim_token`/`fingerprint`/`phone` 由工具 redeem 后自动写回。启动时 redeem CDK 换 claimToken 取号，每轮 `GET /allocations/{id}/messages` 轮询取码，token 过期自动 re-redeem |
| `email_sources` | 邮箱取件来源数组；每项含 `label`、`email`、`provider`（目前仅 `icloud`）、`base_url`（默认 `https://email.nloop.cc`）。走 `POST {base_url}/api/{provider}/query` 拉最新邮件并自动提取验证码 |
| `accounts` | 账户档案数组；每项含 `label`、`login_email`、`password`、`totp_secret`、`phone`、`phone_source_label`、`email`。面板会用标准库实时计算 6 位 TOTP 并显示剩余秒数，`phone_source_label` 优先显式关联动态/固定短信来源，未配置时继续用 `phone` 匹配固定来源 |
| `disabled` | 无效来源/账户的持久化标记字典；key 为 `ludan` 或 `<kind>:<label>`，值含 `reason` / `at`。由监控自动写入或 `config disable` / `enable` / `prune` 管理，一般不手改 |

> `config.json` 含真实 key、接码链接 token、账户密码和 2FA 密钥，已被 `.gitignore` 忽略，不会提交。账户密码和 2FA 密钥属于明文存储，仅适合本机临时使用；`config.example.json` 只能放占位值。
