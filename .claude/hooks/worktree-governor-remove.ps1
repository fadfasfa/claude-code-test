<#
中文简介：只清理 registry 记录中归属 agent 的临时 worktree。
何时读取：当 Claude Code 通过 WorktreeRemove hook 尝试移除隔离 worktree 时读取。
约束内容：必须校验 marker 归属、受管 auto 路径、干净 git 状态，并且只使用非强制 git worktree remove。
不负责：不删除分支、不强制清理，也不清理用户或持久 worktree。
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
$AutoRoot = Join-Path $ManagedRoot "auto"
$RegistryRoot = Join-Path $ManagedRoot ".registry"

# 向 stderr 输出治理决策或失败原因。
function Write-Err([string]$Message) {
  [Console]::Error.WriteLine($Message)
}

# 统一失败出口：记录原因并返回指定退出码。
function Fail([string]$Message, [int]$Code = 1) {
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

# 解析为绝对路径并去除尾部分隔符，用于稳定比较。
function Normalize-PathText([string]$PathText) {
  $converted = Convert-PathText $PathText
  if ([string]::IsNullOrWhiteSpace($converted)) { return "" }
  return ([System.IO.Path]::GetFullPath($converted)).TrimEnd('\', '/')
}

# 仅用路径文本判断 Child 是否位于 Parent 下。
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

# 根据 worktree 路径查找对应 marker 记录。
function Get-MarkerForPath([string]$WorktreePath) {
  if (-not (Test-Path -LiteralPath $RegistryRoot)) { return $null }
  $target = Normalize-PathText $WorktreePath
  foreach ($file in (Get-ChildItem -LiteralPath $RegistryRoot -Filter "*.json" -File -ErrorAction SilentlyContinue)) {
    try {
      $marker = Get-Content -LiteralPath $file.FullName -Raw | ConvertFrom-Json
    }
    catch {
      Write-Err "worktree-governor: ignoring unreadable registry marker: $($file.FullName)"
      continue
    }
    $markerPath = Normalize-PathText ([string](Get-JsonField $marker @("path")))
    if ($target.Equals($markerPath, [System.StringComparison]::OrdinalIgnoreCase)) {
      return [pscustomobject]@{
        File = $file.FullName
        Data = $marker
      }
    }
  }
  return $null
}

# 将清理决策写回 marker，保留后续审计线索。
function Save-Marker($MarkerRecord, [string]$Status, [string]$Reason) {
  if (-not $MarkerRecord) { return }
  $data = $MarkerRecord.Data
  $data | Add-Member -NotePropertyName "cleanup_status" -NotePropertyValue $Status -Force
  $data | Add-Member -NotePropertyName "cleanup_reason" -NotePropertyValue $Reason -Force
  $data | Add-Member -NotePropertyName "cleanup_checked_at" -NotePropertyValue ((Get-Date).ToUniversalTime().ToString("o")) -Force
  $data | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $MarkerRecord.File -Encoding UTF8
}

$stdin = [Console]::In.ReadToEnd()
$event = $null
if (-not [string]::IsNullOrWhiteSpace($stdin)) {
  try {
    $event = $stdin | ConvertFrom-Json
  }
  catch {
    Fail "worktree-governor: invalid WorktreeRemove JSON: $($_.Exception.Message)"
  }
}

$worktreePath = [string](Get-JsonField $event @("worktree_path", "path"))
if ([string]::IsNullOrWhiteSpace($worktreePath) -and $event -and ($event.PSObject.Properties.Name -contains "tool_input")) {
  $worktreePath = [string](Get-JsonField $event.tool_input @("worktree_path", "path"))
}
if ([string]::IsNullOrWhiteSpace($worktreePath)) {
  Fail "worktree-governor: WorktreeRemove payload is missing worktree_path"
}

$worktreePath = Normalize-PathText $worktreePath
$markerRecord = Get-MarkerForPath $worktreePath
if (-not $markerRecord) {
  Write-Err "worktree-governor: skip cleanup; no registry marker for path=$worktreePath"
  exit 0
}

$marker = $markerRecord.Data
$owner = [string](Get-JsonField $marker @("owner"))
$protected = [bool](Get-JsonField $marker @("protected"))
if ($owner -ne "agent" -or $protected) {
  Save-Marker $markerRecord "skipped" "owner is not agent or marker is protected"
  Write-Err "worktree-governor: skip cleanup; owner=$owner protected=$protected path=$worktreePath"
  exit 0
}

if (-not (Test-UnderPath $worktreePath $AutoRoot)) {
  Save-Marker $markerRecord "blocked" "path is outside managed auto root"
  Write-Err "worktree-governor: blocked cleanup; path is outside managed auto root: $worktreePath"
  exit 0
}

if (-not (Test-Path -LiteralPath $worktreePath)) {
  Save-Marker $markerRecord "skipped" "path does not exist"
  Write-Err "worktree-governor: skip cleanup; path does not exist: $worktreePath"
  exit 0
}

$oldErrorActionPreference = $ErrorActionPreference
$ErrorActionPreference = "Continue"
try {
  $status = git -C $worktreePath status --porcelain --untracked-files=all 2>&1
  $statusExit = $LASTEXITCODE
}
finally {
  $ErrorActionPreference = $oldErrorActionPreference
}
if ($statusExit -ne 0) {
  Save-Marker $markerRecord "blocked" "git status failed"
  Write-Err "worktree-governor: blocked cleanup; git status failed for path=$worktreePath"
  foreach ($line in $status) {
    if (-not [string]::IsNullOrWhiteSpace([string]$line)) { Write-Err ([string]$line) }
  }
  exit 0
}

$dirty = @($status | Where-Object { $_ -match '\S' })
if ($dirty.Count -gt 0) {
  Save-Marker $markerRecord "blocked_dirty" "worktree has uncommitted or untracked changes"
  Write-Err "worktree-governor: dirty ephemeral worktree not removed: $worktreePath"
  foreach ($line in $dirty) {
    Write-Err "  $line"
  }
  exit 0
}

$oldErrorActionPreference = $ErrorActionPreference
$ErrorActionPreference = "Continue"
try {
  $removeOutput = git -C $RepoRoot worktree remove $worktreePath 2>&1
  $removeExit = $LASTEXITCODE
}
finally {
  $ErrorActionPreference = $oldErrorActionPreference
}
foreach ($line in $removeOutput) {
  if (-not [string]::IsNullOrWhiteSpace([string]$line)) { Write-Err ([string]$line) }
}
if ($removeExit -ne 0) {
  Save-Marker $markerRecord "blocked" "git worktree remove failed"
  Fail "worktree-governor: git worktree remove failed for path=$worktreePath" 1
}

Save-Marker $markerRecord "removed" "clean owner=agent worktree removed; branch retained"
Write-Err "worktree-governor: removed clean ephemeral worktree; branch retained: $worktreePath"
exit 0
