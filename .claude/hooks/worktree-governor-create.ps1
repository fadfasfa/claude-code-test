<#
中文简介：约束 Claude Code WorktreeCreate 请求，只允许受管路径和受控名称。
何时读取：当 Claude Code 通过 WorktreeCreate hook 创建隔离 worktree 时读取。
约束内容：防止嵌套 worktree，强制 directed/auto 命名，并把新 worktree 放到受管根目录。
不负责：不删除 worktree、不清理旧目录，也不替代人工确认。
#>

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$RepoRoot = "C:\Users\apple\claudecode"
$ManagedRoot = "C:\Users\apple\_worktrees\claudecode"
if ($env:WORKTREE_GOVERNOR_TEST_MODE -eq "1") {
  if (-not [string]::IsNullOrWhiteSpace($env:WORKTREE_GOVERNOR_REPO_ROOT)) {
    $RepoRoot = $env:WORKTREE_GOVERNOR_REPO_ROOT
  }
  if (-not [string]::IsNullOrWhiteSpace($env:WORKTREE_GOVERNOR_MANAGED_ROOT)) {
    $ManagedRoot = $env:WORKTREE_GOVERNOR_MANAGED_ROOT
  }
}
$LegacyRoot = Join-Path $RepoRoot ".claude\worktrees"
$DirectedRoot = Join-Path $ManagedRoot "directed"
$AutoRoot = Join-Path $ManagedRoot "auto"
$RegistryRoot = Join-Path $ManagedRoot ".registry"

function Write-Err([string]$Message) {
  [Console]::Error.WriteLine($Message)
}

# 统一失败出口：报告原因并停止 hook；不自动修复。
function Fail([string]$Message, [int]$Code = 2) {
  Write-Err $Message
  exit $Code
}

# 将 WSL 风格路径转换为 Windows 路径文本。
function Convert-PathText([string]$PathText) {
  if ([string]::IsNullOrWhiteSpace($PathText)) { return "" }
  if ($PathText -match '^/mnt/([a-zA-Z])/(.*)$') {
    return ("{0}:\{1}" -f $matches[1].ToUpperInvariant(), ($matches[2] -replace '/', '\'))
  }
  return $PathText
}

# 解析为绝对路径并去除尾部分隔符，用于边界检查。
function Normalize-PathText([string]$PathText) {
  $converted = Convert-PathText $PathText
  if ([string]::IsNullOrWhiteSpace($converted)) { return "" }
  return ([System.IO.Path]::GetFullPath($converted)).TrimEnd('\', '/')
}

# 仅用路径文本判断 Child 是否位于 Parent 下；不读取业务文件。
function Test-UnderPath([string]$Child, [string]$Parent) {
  $c = Normalize-PathText $Child
  $p = Normalize-PathText $Parent
  if ([string]::IsNullOrWhiteSpace($c) -or [string]::IsNullOrWhiteSpace($p)) { return $false }
  return $c.Equals($p, [System.StringComparison]::OrdinalIgnoreCase) -or
    $c.StartsWith($p + "\", [System.StringComparison]::OrdinalIgnoreCase)
}

# 从不同事件 payload 形态中提取字段。
function Get-JsonField($Object, [string[]]$Names) {
  if (-not $Object) { return $null }
  foreach ($name in $Names) {
    if ($Object.PSObject.Properties.Name -contains $name) {
      return $Object.$name
    }
  }
  return $null
}

# 将 session id 压缩为安全短 token，用于自动 worktree 分支名。
function Get-ShortSessionId([string]$SessionId) {
  $sid = ($SessionId.ToLowerInvariant() -replace '[^a-z0-9]', '')
  if ([string]::IsNullOrWhiteSpace($sid)) { return "nosession" }
  if ($sid.Length -gt 8) { return $sid.Substring(0, 8) }
  return $sid
}

# 生成适合路径和分支名使用的短 slug。
function Get-Slug([string]$Text, [string]$Fallback) {
  $slug = ($Text.ToLowerInvariant() -replace '[^a-z0-9]+', '-')
  $slug = ($slug -replace '-{2,}', '-').Trim('-')
  if ([string]::IsNullOrWhiteSpace($slug)) { $slug = $Fallback }
  if ($slug.Length -gt 80) { $slug = $slug.Substring(0, 80).Trim('-') }
  if ($slug -notmatch '^[a-z0-9][a-z0-9-]{2,80}$') {
    Fail "worktree-governor: invalid worktree purpose after slug normalization: $Text"
  }
  return $slug
}

# 生成 registry 文件名 token，避免任意字符进入路径。
function Get-RegistryToken([string]$Text, [string]$Fallback) {
  $token = ($Text.ToLowerInvariant() -replace '[^a-z0-9._-]+', '-')
  $token = ($token -replace '-{2,}', '-').Trim('-')
  if ([string]::IsNullOrWhiteSpace($token)) { return $Fallback }
  if ($token.Length -gt 96) { return $token.Substring(0, 96).Trim('-') }
  return $token
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
$agentId = [string](Get-JsonField $event @("agent_id", "agentId"))
$agentType = [string](Get-JsonField $event @("agent_type", "agentType"))
if ([string]::IsNullOrWhiteSpace($name) -and $event -and ($event.PSObject.Properties.Name -contains "tool_input")) {
  $name = [string](Get-JsonField $event.tool_input @("name", "worktree_name"))
}
if ([string]::IsNullOrWhiteSpace($cwd) -and $event -and ($event.PSObject.Properties.Name -contains "tool_input")) {
  $cwd = [string](Get-JsonField $event.tool_input @("cwd", "current_working_directory"))
}
if ([string]::IsNullOrWhiteSpace($sessionId) -and $event -and ($event.PSObject.Properties.Name -contains "tool_input")) {
  $sessionId = [string](Get-JsonField $event.tool_input @("session_id", "sessionId"))
}
if ([string]::IsNullOrWhiteSpace($agentId) -and $event -and ($event.PSObject.Properties.Name -contains "tool_input")) {
  $agentId = [string](Get-JsonField $event.tool_input @("agent_id", "agentId"))
}
if ([string]::IsNullOrWhiteSpace($agentType) -and $event -and ($event.PSObject.Properties.Name -contains "tool_input")) {
  $agentType = [string](Get-JsonField $event.tool_input @("agent_type", "agentType"))
}
if ([string]::IsNullOrWhiteSpace($cwd)) {
  $cwd = (Get-Location).Path
}

if (Test-UnderPath $cwd $LegacyRoot -or Test-UnderPath $cwd $ManagedRoot) {
  Fail "worktree-governor: nested worktree creation is forbidden from cwd=$cwd"
}

if ([string]::IsNullOrWhiteSpace($name)) {
  Fail "worktree-governor: WorktreeCreate payload is missing name"
}

$owner = "agent"
$protected = $false
$branch = $null
$path = $null
if ($name -match '^(directed|user)-(?<purpose>[a-z0-9][a-z0-9-]{2,80})$') {
  $owner = "user"
  $protected = $true
  $purpose = $matches.purpose
  $branch = "wt-directed-$purpose"
  $path = Join-Path $DirectedRoot $purpose
}
elseif ($name -match '^(auto|cc-auto)-(?<purpose>[a-z0-9][a-z0-9-]{2,80})$') {
  $owner = "agent"
  $purpose = $matches.purpose
  $shortSession = Get-ShortSessionId $sessionId
  $stamp = Get-Date -Format "yyyyMMdd-HHmm"
  $branch = "wt-auto-cc-$stamp-$purpose-$shortSession"
  $path = Join-Path $AutoRoot $branch
}
else {
  $owner = "agent"
  $purpose = Get-Slug $name "worktree"
  $shortSession = Get-ShortSessionId $sessionId
  $stamp = Get-Date -Format "yyyyMMdd-HHmm"
  $branch = "wt-auto-cc-$stamp-$purpose-$shortSession"
  $path = Join-Path $AutoRoot $branch
}

$path = Normalize-PathText $path
$safeSession = Get-RegistryToken $sessionId "nosession"
$safeName = Get-RegistryToken $name "noname"
$registryPath = Join-Path $RegistryRoot "$safeSession-$safeName.json"
$marker = [pscustomobject]@{
  owner = $owner
  protected = $protected
  session_id = $sessionId
  agent_id = $agentId
  agent_type = $agentType
  name = $name
  purpose = $purpose
  path = $path
  branch = $branch
  created_at = (Get-Date).ToUniversalTime().ToString("o")
  cleanup_policy = if ($owner -eq "agent") { "remove_worktree_clean_only_keep_branch_report_dirty" } else { "persistent_skip_cleanup" }
}

if ($env:WORKTREE_GOVERNOR_DRY_RUN -eq "1") {
  if ($env:WORKTREE_GOVERNOR_DRY_RUN_JSON -eq "1") {
    $marker | ConvertTo-Json -Depth 5
  }
  else {
    [Console]::Out.WriteLine($path)
  }
  exit 0
}

if (-not (Test-Path -LiteralPath $RegistryRoot)) {
  New-Item -ItemType Directory -Path $RegistryRoot | Out-Null
}

if (Test-Path -LiteralPath $registryPath) {
  Fail "worktree-governor: registry marker already exists: $registryPath"
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

$oldErrorActionPreference = $ErrorActionPreference
$ErrorActionPreference = "Continue"
try {
  $gitOutput = git -C $RepoRoot worktree add -b $branch $path HEAD 2>&1
  $gitExit = $LASTEXITCODE
}
finally {
  $ErrorActionPreference = $oldErrorActionPreference
}
foreach ($line in $gitOutput) {
  if (-not [string]::IsNullOrWhiteSpace([string]$line)) { Write-Err ([string]$line) }
}
if ($gitExit -ne 0) {
  Fail "worktree-governor: git worktree add failed for branch=$branch path=$path" 1
}

$marker | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $registryPath -Encoding UTF8
[Console]::Out.WriteLine($path)
