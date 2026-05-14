<#
中文简介：
- 这个文件是什么：workflow 脚本共享的 task metadata 读写和 schema 校验工具。
- 什么时候读：worktree-status、local-review、finalize-pr、cleanup-worktree 需要读取 `.task-worktree.json` 时。
- 约束什么：缺少固定 schema 字段时必须失败，避免 finalize 使用过期或不完整的验收记录。
#>

Set-StrictMode -Version Latest

$script:TaskMetadataRequiredFields = @(
  "schema_version",
  "repo_name",
  "main_repo_path",
  "worktree_path",
  "task_slug",
  "target_paths",
  "base_ref",
  "base_commit",
  "mode",
  "main_dirty_snapshot",
  "acceptance_gate",
  "manual_required",
  "manual_accepted",
  "review_branch",
  "created_at",
  "updated_at"
)

function Resolve-WorkflowRepoRoot {
  $root = git rev-parse --show-toplevel 2>$null
  if (-not $root) { throw "未在 Git 仓库内，无法解析 repo root。" }
  return (Resolve-Path -LiteralPath $root).Path
}

function Get-TaskMetadataPath {
  param([string]$RepoRoot)
  return (Join-Path $RepoRoot ".task-worktree.json")
}

function Get-TaskHandoffPath {
  param([string]$RepoRoot)
  return (Join-Path $RepoRoot "TASK_HANDOFF.md")
}

function Test-IsTaskWorktreePath {
  param([string]$Path)
  $resolved = (Resolve-Path -LiteralPath $Path -ErrorAction Stop).Path
  return $resolved.StartsWith("C:\Users\apple\worktrees", [System.StringComparison]::OrdinalIgnoreCase) -or
    $resolved.StartsWith("C:\Users\apple\_worktrees", [System.StringComparison]::OrdinalIgnoreCase)
}

function ConvertTo-RelativeRepoPath {
  param(
    [string]$RepoRoot,
    [string]$Path
  )
  $normalizedRoot = $RepoRoot.TrimEnd('\', '/')
  $full = if ([System.IO.Path]::IsPathRooted($Path)) { $Path } else { Join-Path $RepoRoot $Path }
  $resolved = [System.IO.Path]::GetFullPath($full)
  if (-not $resolved.StartsWith($normalizedRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw "路径不在 repo 内：$Path"
  }
  return $resolved.Substring($normalizedRoot.Length).TrimStart('\', '/') -replace '\\', '/'
}

function Read-TaskMetadata {
  param(
    [string]$RepoRoot = (Resolve-WorkflowRepoRoot),
    [switch]$Require
  )
  $path = Get-TaskMetadataPath -RepoRoot $RepoRoot
  if (-not (Test-Path -LiteralPath $path)) {
    if ($Require) { throw "缺少 .task-worktree.json：$path" }
    return $null
  }
  $metadata = Get-Content -LiteralPath $path -Raw | ConvertFrom-Json
  $missing = @()
  foreach ($field in $script:TaskMetadataRequiredFields) {
    if (-not ($metadata.PSObject.Properties.Name -contains $field)) { $missing += $field }
  }
  if ($missing.Count -gt 0) {
    throw ".task-worktree.json 缺少字段：$($missing -join ', ')"
  }
  return $metadata
}

function Write-TaskMetadata {
  param(
    [Parameter(Mandatory = $true)]$Metadata,
    [string]$RepoRoot = (Resolve-WorkflowRepoRoot)
  )
  $Metadata.updated_at = (Get-Date).ToUniversalTime().ToString("o")
  $path = Get-TaskMetadataPath -RepoRoot $RepoRoot
  $Metadata | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $path -Encoding UTF8
  [void](Read-TaskMetadata -RepoRoot $RepoRoot -Require)
}

function Get-WorkflowChangedFiles {
  param([string]$RepoRoot = (Resolve-WorkflowRepoRoot))
  $status = @(git -C $RepoRoot status --porcelain)
  $files = @()
  foreach ($line in $status) {
    if ($line.Length -lt 4) { continue }
    $path = $line.Substring(3).Trim()
    if ($path -match ' -> ') { $path = ($path -split ' -> ')[-1].Trim() }
    $files += [pscustomobject]@{
      Status = $line.Substring(0, 2)
      Path = ($path -replace '\\', '/')
      Raw = $line
    }
  }
  return @($files)
}

function Test-PathWithinScopes {
  param(
    [string]$Path,
    [object[]]$Scopes
  )
  $p = ($Path -replace '\\', '/').TrimStart('/')
  foreach ($scope in $Scopes) {
    $s = ([string]$scope -replace '\\', '/').TrimStart('/').TrimEnd('/')
    if ([string]::IsNullOrWhiteSpace($s) -or $s -eq ".") { return $true }
    if ($p -eq $s -or $p.StartsWith("$s/", [System.StringComparison]::OrdinalIgnoreCase)) { return $true }
  }
  return $false
}

function Test-ProtectedOrConfigPath {
  param([string]$Path)
  $p = ($Path -replace '\\', '/').ToLowerInvariant()
  return (
    $p -match '^run/data/raw/' -or
    $p -match '(^|/)auth\.json$' -or
    $p -match '(^|/)\.env($|\.)' -or
    $p -match 'token|cookie|api[_-]?key|proxy[_-]?secret' -or
    $p -match '(^|/)local\.yaml$' -or
    $p -match '(^|/)proxies\.json$' -or
    $p -match '^\.codex/' -or
    $p -match '^\.claude/' -or
    $p -match '^agents\.md$|^\.agents/skills/|^scripts/workflow/|^scripts/git/'
  )
}

function Test-ManualRequiredPath {
  param([string]$Path)
  $p = ($Path -replace '\\', '/').ToLowerInvariant()
  return (
    (Test-ProtectedOrConfigPath -Path $Path) -or
    $p -match 'display/static|frontend|ui|style|css|chart|layout' -or
    $p -match '^run/data/|scrap|crawler|spider' -or
    $p -match '^work_area_registry\.md$|^docs/workflows/'
  )
}
