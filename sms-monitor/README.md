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

> `config.json` 含真实 key、接码链接 token、账户密码和 2FA 密钥，已被 `.gitignore` 忽略，不会提交。账户密码和 2FA 密钥属于明文存储，仅适合本机临时使用；`config.example.json` 只能放占位值。
