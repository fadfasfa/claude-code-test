<#
中文简介：repo-local WorktreeCreate hook，负责把 Claude Code worktree 请求收束到受管路径和命名规则。
什么时候读：需要理解本仓 worktree 创建护栏时读取。
约束什么：禁止从已有 worktree 内嵌套创建，限制 directed/auto 命名，并把新 worktree 放到受管根目录。
不负责什么：不删除 worktree，不清理 legacy worktree，不替代人工确认。
#>

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$RepoRoot = "C:\Users\apple\claudecode"
$LegacyRoot = "C:\Users\apple\claudecode\.claude\worktrees"
$ManagedRoot = "C:\Users\apple\_worktrees\claudecode"
$DirectedRoot = Join-Path $ManagedRoot "directed"
$AutoRoot = Join-Path $ManagedRoot "auto"

function Write-Err([string]$Message) {
  [Console]::Error.WriteLine($Message)
}

# 统一失败出口：报告原因并停止当前 hook，不做自动修复。
function Fail([string]$Message, [int]$Code = 2) {
  Write-Err $Message
  exit $Code
}

# 兼容 WSL 风格路径，统一转换为 Windows 路径文本。
function Convert-PathText([string]$PathText) {
  if ([string]::IsNullOrWhiteSpace($PathText)) { return "" }
  if ($PathText -match '^/mnt/([a-zA-Z])/(.*)$') {
    return ("{0}:\{1}" -f $matches[1].ToUpperInvariant(), ($matches[2] -replace '/', '\'))
  }
  return $PathText
}

# 解析为绝对路径并去掉末尾分隔符，用于后续边界判断。
function Normalize-PathText([string]$PathText) {
  $converted = Convert-PathText $PathText
  if ([string]::IsNullOrWhiteSpace($converted)) { return "" }
  return ([System.IO.Path]::GetFullPath($converted)).TrimEnd('\', '/')
}

# 判断 Child 是否位于 Parent 之下；输入输出都是路径文本，不访问业务文件内容。
function Test-UnderPath([string]$Child, [string]$Parent) {
  $c = Normalize-PathText $Child
  $p = Normalize-PathText $Parent
  if ([string]::IsNullOrWhiteSpace($c) -or [string]::IsNullOrWhiteSpace($p)) { return $false }
  return $c.Equals($p, [System.StringComparison]::OrdinalIgnoreCase) -or
    $c.StartsWith($p + "\", [System.StringComparison]::OrdinalIgnoreCase)
}

# 从不同事件 payload 形态里提取字段。
function Get-JsonField($Object, [string[]]$Names) {
  if (-not $Object) { return $null }
  foreach ($name in $Names) {
    if ($Object.PSObject.Properties.Name -contains $name) {
      return $Object.$name
    }
  }
  return $null
}

# 将 session id 压缩为安全短标识，用于 auto worktree 分支名。
function Get-ShortSessionId([string]$SessionId) {
  $sid = ($SessionId.ToLowerInvariant() -replace '[^a-z0-9]', '')
  if ([string]::IsNullOrWhiteSpace($sid)) { return "nosession" }
  if ($sid.Length -gt 8) { return $sid.Substring(0, 8) }
  return $sid
}

$stdin = [Console]::In.ReadToEnd()
$event = $null
if (-not [string]::IsNullOrWhiteSpace($stdin)) {
  try {
    $event = $stdin | ConvertFrom-Json
  }
  catch {
    Fail "worktree-governor: invalid WorktreeCreate JSON: $($_.Exception.Message)"
  }
}

$name = [string](Get-JsonField $event @("name", "worktree_name"))
$cwd = [string](Get-JsonField $event @("cwd", "current_working_directory"))
$sessionId = [string](Get-JsonField $event @("session_id", "sessionId"))
if ([string]::IsNullOrWhiteSpace($name) -and $event -and ($event.PSObject.Properties.Name -contains "tool_input")) {
  $name = [string](Get-JsonField $event.tool_input @("name", "worktree_name"))
}
if ([string]::IsNullOrWhiteSpace($cwd) -and $event -and ($event.PSObject.Properties.Name -contains "tool_input")) {
  $cwd = [string](Get-JsonField $event.tool_input @("cwd", "current_working_directory"))
}
if ([string]::IsNullOrWhiteSpace($sessionId) -and $event -and ($event.PSObject.Properties.Name -contains "tool_input")) {
  $sessionId = [string](Get-JsonField $event.tool_input @("session_id", "sessionId"))
}
if ([string]::IsNullOrWhiteSpace($cwd)) {
  $cwd = (Get-Location).Path
}

if (Test-UnderPath $cwd $LegacyRoot -or Test-UnderPath $cwd $ManagedRoot) {
  Fail "worktree-governor: nested worktree creation is forbidden from cwd=$cwd"
}

if ($name -notmatch '^directed-[a-z0-9][a-z0-9-]{2,80}$' -and
    $name -notmatch '^(auto|cc-auto)-[a-z0-9][a-z0-9-]{2,80}$') {
  Fail "worktree-governor: invalid worktree name. directed: use directed-<purpose>; auto: use auto-<purpose> or cc-auto-<purpose>"
}

$branch = $null
$path = $null
if ($name -match '^directed-(?<purpose>[a-z0-9][a-z0-9-]{2,80})$') {
  $purpose = $matches.purpose
  $branch = "wt-directed-$purpose"
  $path = Join-Path $DirectedRoot $purpose
}
elseif ($name -match '^(auto|cc-auto)-(?<purpose>[a-z0-9][a-z0-9-]{2,80})$') {
  $purpose = $matches.purpose
  $shortSession = Get-ShortSessionId $sessionId
  $stamp = Get-Date -Format "yyyyMMdd-HHmm"
  $branch = "wt-auto-cc-$stamp-$purpose-$shortSession"
  $path = Join-Path $AutoRoot $branch
}

$path = Normalize-PathText $path

if ($env:WORKTREE_GOVERNOR_DRY_RUN -eq "1") {
  [Console]::Out.WriteLine($path)
  exit 0
}

git -C $RepoRoot show-ref --verify --quiet "refs/heads/$branch"
$showRefExit = $LASTEXITCODE
if ($showRefExit -eq 0) {
  Fail "worktree-governor: branch already exists: $branch"
}
elseif ($showRefExit -ne 1) {
  Fail "worktree-governor: failed to check branch existence: $branch"
}

if (Test-Path -LiteralPath $path) {
  Fail "worktree-governor: worktree path already exists: $path"
}

$parent = Split-Path -Parent $path
if (-not (Test-Path -LiteralPath $parent)) {
  New-Item -ItemType Directory -Path $parent | Out-Null
}

$gitOutput = git -C $RepoRoot worktree add -b $branch $path HEAD 2>&1
$gitExit = $LASTEXITCODE
foreach ($line in $gitOutput) {
  if (-not [string]::IsNullOrWhiteSpace([string]$line)) { Write-Err ([string]$line) }
}
if ($gitExit -ne 0) {
  Fail "worktree-governor: git worktree add failed for branch=$branch path=$path" 1
}

[Console]::Out.WriteLine($path)
