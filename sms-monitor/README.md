# SMS 验证码多来源监控

实时轮询 LuDan SMS 接码平台、固定文本接码链接和邮箱取件接口，终端按来源显示号码/邮箱与最新验证码，并可在同一面板展示本机账户档案。

## 使用

1. 首次使用：复制 `config.example.json` 为 `config.json`，把 `key` 改成你的 CDK，并把 `fixed_sources` 里的 `url` 改成真实接码链接。
2. 双击 `run.bat` 启动（或命令行 `python monitor.py`）。
3. 面板会显示 LuDan 动态号码、未归属账户的固定号码/邮箱来源和账户档案；已被账户关联的接码来源会聚合到账户卡片内，不在顶层重复列出。美国号码会拆成国家码 `+1` 与 10 位号码，复制区域只放 10 位号码。
4. 按来源序号手动复制对应 10 位号码/邮箱；检测到新的手机或邮箱验证码时会自动复制验证码。账户档案按序号后会进入复制子菜单，可复制登录邮箱、当前 2FA 动态码和 10 位手机号码；无 TOTP 密钥或密钥无效时不显示动态码复制项。
5. 启动后默认低频刷新，方便滚动查看历史；复制号码/邮箱/账户项或手动换号后，会进入高频轮询，拿到新验证码并自动复制后回到低频。多个接码来源会并发轮询，单个慢接口只显示本轮超时提示，不会拖住整轮刷新。

## 标准录入流程

给 Codex / Claude Code 执行录入时，优先使用 `config` 子命令，不直接手改 `config.json`。

1. 结构检查：`python monitor.py config validate --json`
2. 初始化或更新全局配置：`python monitor.py config init --json`，再用 `python monitor.py config set-global --key-env SMS_MONITOR_KEY --json`
3. 录入固定短信来源：`python monitor.py config upsert-fixed --label YunTL --phone 15550123456 --url-env FIXED_URL --json`
4. 录入邮箱来源：`python monitor.py config upsert-email --label iCloud --email example@icloud.com --provider icloud --base-url https://email.nloop.cc --json`
5. 录入账户档案：`python monitor.py config upsert-account --label ChatGPT --login-email your-gmail@example.com --password-env ACCOUNT_PASSWORD --totp-secret-env ACCOUNT_TOTP --phone 15550123456 --email example@icloud.com --json`
6. 预备接码检查：`python monitor.py config ready-check --all --json`

手机号可按原始格式录入用于来源匹配，但登录输入或复制时只使用美国 10 位本地号码，不要把 `+1` 等国家码计入目标输入框。

`ready-check` 的 `ready=true` 表示来源已经可等待验证码，不表示已经收到验证码。命令输出只包含来源 label、类型、状态和脱敏原因；真实 key、URL token、密码和 TOTP 密钥必须通过环境变量传入，不应出现在命令输出或对话里。

### 无效来源与账户管理

LuDan 卡密失效（CDK 校验失败 403）或某固定/邮箱来源连续硬失败（HTTP 401/403/404/410 或网络异常，连续 5 次）时，监控会自动把该项标记为无效并持久化到 `config.json` 的 `disabled` 字典，跳过轮询但保留展示，不再因单个失效来源退出整个程序。429 / 5xx / 超时 / 暂无短信等临时状态不会触发禁用。面板底部会列出当前已禁用项及原因。

手动管理无效项（`--kind` 取值 `ludan` / `fixed` / `email` / `account`，`ludan` 的 `--label` 可传任意占位值）：

```powershell
python monitor.py config list-disabled --json                                # 列出所有无效项
python monitor.py config disable --label eSIM88 --kind fixed --reason "链接失效" --json
python monitor.py config enable  --label eSIM88 --kind fixed --json          # 恢复轮询
python monitor.py config prune  --json                                        # 预览将清理的无效项
python monitor.py config prune  --yes --json                                  # 从 config 物理删除无效的固定/邮箱/账户项
```

`prune --yes` 会删除 `disabled` 标记的固定/邮箱/账户项并清空 `disabled` 字典；LuDan 是顶层配置无法物理删除，`prune` 后其失效标记一并清除，下次启动会重新校验。定期用 `prune --yes` 清理无效账号即可。

### 自由文本导入

如果来源文本不标准，不要把原文贴进对话。先把原文复制到本机剪贴板，然后运行：

```powershell
python monitor.py config import-freeform --from-clipboard --label ChatGPT-1 --interactive --json
```

也可以在本机终端粘贴到标准输入：

```powershell
python monitor.py config import-freeform --stdin --label ChatGPT-1 --interactive --json
```

脚本会在本机内存中解析 `账号 / 密码 / 2FA / 手机号 / 接码 URL`，只显示脱敏预览；解析缺字段时会在终端本地提示补齐。确认预览正确后再加 `--yes` 执行写入：

```powershell
python monitor.py config import-freeform --from-clipboard --label ChatGPT-1 --interactive --yes --json
python monitor.py config ready-check --all --json
```

## 用 Claude Code 导入

本节是给 Claude Code（CC）用的标准导入流程。用户把账号/电话信息贴给 CC，CC 按本节格式解析并调用 `config` 子命令录入；敏感值（密码 / TOTP / 接码 URL token）一律走环境变量，不进命令行明文、不回显到对话。

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

### 多账号

- **逐个串行执行，不要并行**：多账号改同一 `config.json`，read-modify-write 非原子，并行会互相覆盖。
- 每个账号用独立环境变量名（如 `ACC1_*` / `ACC2_*`）避免串值。

### 导入后校验

```powershell
python monitor.py config validate    --json
python monitor.py config ready-check  --all --json
```

- `validate`：本地结构校验。
- `ready-check --all`：LuDan + 全部固定 / 邮箱来源 + 账户关联；`ready=true` 即就绪；新接码链接应返回 `ready`（如「暂无短信」= 链接可达、暂无验证码）。
- 两者输出均脱敏，不含明文 secret。

### 注意事项

- 不手改 `config.json`：一律走 `config` 子命令（见上文「标准录入流程」+ `AGENTS.md` 凭据保护）。
- LuDan 卡密失效（403）不再退出整个程序：自动标记无效、跳过轮询、继续展示固定来源和账户；换 CDK 后用 `config enable --kind ludan --label LuDan` 恢复（已禁用项不会重新校验，需先 enable 才会再校验）。
- 敏感值不进命令行明文、不回显：用 `--*-env` 传环境变量；命令输出只有脱敏预览。
- 为何不用 `import-freeform` 作主路径：其文本解析器对「中文 label 多行格式」（`邮箱：/密码：/2fa:`）不可靠，密码字段会被前缀污染；结构化命令每字段显式传值，可靠。`import-freeform` 仅适合「紧凑单行 `----` 分隔格式」（见上方「自由文本导入」节）。
- 手机号原样录入用于关联；显示 / 复制时 `split_us_phone` 只取美国 10 位本地号。
- 取码方式覆盖范围：当前仅支持①固定 URL 直接出码（`fixed_sources`）与②LuDan 动态号（`LuDanSource`）。多步换号取码（先输入字符换号、再查码）需扩展代码，本轮不纳入；若遇到此类号码，先确认它是否另有固定取码 URL——有则按场景 A 录入，无则暂缓，等真实样本明确后再评估扩展。

## 依赖

- Python 3 + `requests`（`python -m pip install requests`）。
- 剪贴板与热键功能依赖 Windows（Win32 剪贴板 API，`clip` 作为 fallback，`msvcrt` 负责热键）；其他系统仅 Ctrl+C 退出可用。

## 热键

- 数字键复制对应来源的 10 位号码/邮箱；账户来源会先打开复制子菜单，菜单只列登录邮箱、有效 2FA 动态码和 10 位手机号码
- `n` 仅 LuDan 换号
- `q` / Ctrl+C 退出

## 配置项

| 字段 | 说明 |
| --- | --- |
| `base_url` | 开放 API 地址 |
| `key` | 你的 CDK（接口认证 key） |
| `poll_interval` | 高频轮询间隔秒数（默认 5，限频 45 次/60 秒） |
| `idle_poll_interval` | 低频轮询间隔秒数（默认 15）；未复制前按此节奏刷新，避免终端滚轮被频繁清屏打断 |
| `request_timeout` | 单个 HTTP 请求超时秒数（默认 3）；LuDan、固定链接和邮箱取件共用 |
| `max_poll_workers` | 并发轮询 worker 数（默认 4）；来源多时避免串行等待 |
| `poll_round_timeout` | 单轮并发轮询总等待预算秒数（默认 `request_timeout + 0.5`）；超时来源保留旧状态并显示提示 |
| `active_until_code` | 复制或手动换号后是否一直高频轮询到拿到新验证码（默认 `true`）；拿到验证码后自动回低频 |
| `active_after_copy_seconds` | `active_until_code=false` 时的高频轮询秒数（默认 180） |
| `auto_change_on_expire` | 号码过期时是否自动换号 |
| `fixed_sources` | 固定文本接码链接数组；每项包含 `label`、`phone`、`url` |
| `email_sources` | 邮箱取件来源数组；每项含 `label`、`email`、`provider`（目前仅 `icloud`）、`base_url`（默认 `https://email.nloop.cc`）。走 `POST {base_url}/api/{provider}/query` 拉最新邮件并自动提取验证码 |
| `accounts` | 账户档案数组；每项含 `label`、`login_email`、`password`、`totp_secret`、`phone`、`email`。面板会用标准库实时计算 6 位 TOTP 并显示剩余秒数，`phone` / `email` 用来标注关联的接码来源 |
| `disabled` | 无效来源/账户的持久化标记字典；key 为 `ludan` 或 `<kind>:<label>`，值含 `reason` / `at`。由监控自动写入或 `config disable` / `enable` / `prune` 管理，一般不手改 |

> `config.json` 含真实 key、接码链接 token、账户密码和 2FA 密钥，已被 `.gitignore` 忽略，不会提交。账户密码和 2FA 密钥属于明文存储，仅适合本机临时使用；`config.example.json` 只能放占位值。
