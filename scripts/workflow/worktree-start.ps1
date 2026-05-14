<#
中文简介：
- 这个文件是什么：创建唯一 detached active worktree 的入口。
- 什么时候读：准备进入新任务执行面时。
- 约束什么：默认 dry-run；创建前检查 active worktree、主仓 dirty overlap，并初始化 TASK_HANDOFF / .task-worktree.json。
#>

param(
  [string]$Topic,
  [string]$Area = "task",
  [string]$Base = "main",
  [string]$Root = "C:\Users\apple\worktrees",
  [string]$FallbackRoot = "C:\Users\apple\_worktrees",
  [string[]]$TargetPath,
  [string[]]$AllowedPath,
  [switch]$AllowDirtyOverlapFromHead,
  [switch]$DryRun,
  [switch]$Apply,
  [switch]$Help
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
. "$PSScriptRoot\task-metadata.ps1"

function Show-Help {
  Write-Output "Usage: pwsh -NoProfile -File scripts/workflow/worktree-start.ps1 -Topic <name> -TargetPath <path[]> [-Apply]"
  Write-Output "Default is dry-run. -Apply creates a detached worktree with TASK_HANDOFF.md and .task-worktree.json."
}

function Get-Slug {
  param([string]$InputText)
  $slug = ($InputText.ToLowerInvariant() -replace '[^a-z0-9]+', '-').Trim('-')
  if ([string]::IsNullOrWhiteSpace($slug)) { throw "Topic 经过 slug 化后为空。" }
  return $slug
}

function Get-MainDirtySnapshot {
  param([string]$RepoRoot)
  return @(git -C $RepoRoot status --porcelain | ForEach-Object { $_ })
}

function Get-DirtyPathFromStatus {
  param([string]$Line)
  if ($Line.Length -lt 4) { return $null }
  $path = $Line.Substring(3).Trim()
  if ($path -match ' -> ') { $path = ($path -split ' -> ')[-1].Trim() }
  return ($path -replace '\\', '/')
}

function Get-TaskMetadataFiles {
  $roots = @("C:\Users\apple\worktrees", "C:\Users\apple\_worktrees")
  foreach ($root in $roots) {
    if (-not (Test-Path -LiteralPath $root)) { continue }
    Get-ChildItem -LiteralPath $root -Directory -Force -ErrorAction SilentlyContinue | ForEach-Object {
      $meta = Join-Path $_.FullName ".task-worktree.json"
      if (Test-Path -LiteralPath $meta) { Get-Item -LiteralPath $meta }
    }
  }
}

function Get-RegisteredWorktreePaths {
  param([string]$RepoRoot)
  $paths = @()
  foreach ($line in @(git -C $RepoRoot worktree list --porcelain)) {
    if ($line -match '^worktree\s+(.+)$') { $paths += $Matches[1] }
  }
  return $paths
}

function New-DetachedWorktreeWithFallback {
  param(
    [string]$RepoRoot,
    [string]$PrimaryRoot,
    [string]$FallbackRoot,
    [string]$TaskDirName,
    [string]$BaseRef
  )

  $attempts = @(
    [pscustomobject]@{ Label = "primary"; Root = $PrimaryRoot; Path = (Join-Path $PrimaryRoot $TaskDirName) },
    [pscustomobject]@{ Label = "fallback"; Root = $FallbackRoot; Path = (Join-Path $FallbackRoot $TaskDirName) }
  )
  $failures = @()
  foreach ($attempt in $attempts) {
    if (Test-Path -LiteralPath $attempt.Path) {
      $failures += "$($attempt.Label): target exists: $($attempt.Path)"
      continue
    }
    try {
      New-Item -ItemType Directory -Path $attempt.Root -Force | Out-Null
      git -C $RepoRoot worktree add --detach $attempt.Path $BaseRef
      if ($LASTEXITCODE -eq 0) { return $attempt.Path }
      $failures += "$($attempt.Label): git worktree add --detach exited $LASTEXITCODE"
    } catch {
      $failures += "$($attempt.Label): $($_.Exception.Message)"
    }
  }

  throw "primary/fallback worktree 创建均失败，停止；不会回主仓编码。`n$($failures -join [Environment]::NewLine)"
}

if ($Help) {
  Show-Help
  exit 0
}
if ([string]::IsNullOrWhiteSpace($Topic)) { Show-Help; throw "必须提供 -Topic。" }
if (-not $TargetPath -or $TargetPath.Count -eq 0) { throw "必须提供 -TargetPath，避免任务范围不清。" }
if ($DryRun -and $Apply) { throw "-DryRun 与 -Apply 不能同时使用。" }

$repoRoot = Resolve-WorkflowRepoRoot
$slug = Get-Slug $Topic
$taskDirName = "$Area-$slug"
$target = Join-Path $Root $taskDirName
$fallbackTarget = Join-Path $FallbackRoot $taskDirName
$baseCommit = (git -C $repoRoot rev-parse $Base).Trim()
$dirtySnapshot = Get-MainDirtySnapshot -RepoRoot $repoRoot
$dirtyPaths = @($dirtySnapshot | ForEach-Object { Get-DirtyPathFromStatus -Line $_ } | Where-Object { $_ })
$scopes = @($TargetPath)
$overlap = @($dirtyPaths | Where-Object { Test-PathWithinScopes -Path $_ -Scopes $scopes })

$metadataFiles = @(Get-TaskMetadataFiles)
$registered = @(Get-RegisteredWorktreePaths -RepoRoot $repoRoot)
$activeFindings = @()
foreach ($worktreePath in $registered) {
  $resolved = $worktreePath -replace '/', '\'
  if ($resolved -ieq $repoRoot) { continue }
  if ($resolved.StartsWith("C:\Users\apple\worktrees", [System.StringComparison]::OrdinalIgnoreCase) -or
      $resolved.StartsWith("C:\Users\apple\_worktrees", [System.StringComparison]::OrdinalIgnoreCase)) {
    $dirty = @(git -C $resolved status --porcelain 2>$null)
    $activeFindings += [pscustomobject]@{ Path = $resolved; Dirty = ($dirty.Count -gt 0); DirtyFiles = $dirty }
  }
}
foreach ($file in $metadataFiles) {
  $wt = Split-Path -Parent $file.FullName
  if ($registered -notcontains $wt) {
    $wtDirty = @(git -C $wt status --porcelain 2>$null)
    $activeFindings += [pscustomobject]@{
      Path = $wt
      Dirty = ($wtDirty.Count -gt 0)
      DirtyFiles = $(if ($wtDirty.Count -gt 0) { $wtDirty } else { @("metadata-only") })
    }
  }
}

Write-Output "repo_root: $repoRoot"
Write-Output "target_path: $target"
Write-Output "fallback_target_path: $fallbackTarget"
Write-Output "base: $Base"
Write-Output "base_commit: $baseCommit"
Write-Output "mode: detached"
Write-Output "git_command: git worktree add --detach `"$target`" $Base"
Write-Output "fallback_git_command: git worktree add --detach `"$fallbackTarget`" $Base"
Write-Output "worktree_scan:"
if ($activeFindings.Count -eq 0) { Write-Output "  none" } else { $activeFindings | ForEach-Object { Write-Output "  $($_.Path) dirty=$($_.Dirty)" } }
Write-Output "dirty_overlap:"
if ($overlap.Count -eq 0) { Write-Output "  none" } else { $overlap | ForEach-Object { Write-Output "  $_" } }

if ($activeFindings.Count -gt 0) {
  $dirtyActive = @($activeFindings | Where-Object { $_.Dirty })
  if ($dirtyActive.Count -gt 0) {
    Write-Output "检测到 dirty active worktree，停止创建第二个："
    $dirtyActive | ForEach-Object {
      Write-Output "  path: $($_.Path)"
      $_.DirtyFiles | ForEach-Object { Write-Output "    $_" }
    }
    exit 1
  }
  Write-Output "检测到已有 active worktree，默认不并发创建第二个。"
  exit 1
}

if ($overlap.Count -gt 0 -and -not $AllowDirtyOverlapFromHead) {
  Write-Output "任务目标路径与主仓 dirty 文件重叠，停止。可选路径："
  Write-Output "  1. 先处理主仓脏改。"
  Write-Output "  2. 授权复制指定脏改到 task worktree。"
  Write-Output "  3. 显式传 -AllowDirtyOverlapFromHead 从 HEAD 继续并接受后续冲突。"
  exit 1
}

if ($DryRun -or -not $Apply) {
  Write-Output "schema_preview_fields:"
  $script:TaskMetadataRequiredFields | ForEach-Object { Write-Output "  $_" }
  Write-Output "dry-run: 未创建 worktree。"
  exit 0
}

$createdPath = New-DetachedWorktreeWithFallback -RepoRoot $repoRoot -PrimaryRoot $Root -FallbackRoot $FallbackRoot -TaskDirName $taskDirName -BaseRef $Base

$now = (Get-Date).ToUniversalTime().ToString("o")
$allowed = if ($AllowedPath -and $AllowedPath.Count -gt 0) { $AllowedPath } else { $TargetPath }
$metadata = [pscustomobject]@{
  schema_version = "1.0"
  repo_name = Split-Path -Leaf $repoRoot
  main_repo_path = $repoRoot
  worktree_path = (Resolve-Path -LiteralPath $createdPath).Path
  task_slug = $slug
  target_paths = @($TargetPath)
  allowed_paths = @($allowed)
  base_ref = $Base
  base_commit = $baseCommit
  mode = "detached"
  main_dirty_snapshot = @($dirtySnapshot)
  dirty_overlap_from_head = [bool]$AllowDirtyOverlapFromHead
  acceptance_gate = "manual-required"
  manual_required = $true
  manual_accepted = $false
  review_branch = $null
  approved_changed_files = @()
  verify_status = "not-run"
  local_review_status = "not-run"
  created_at = $now
  updated_at = $now
}
$metadata | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath (Join-Path $createdPath ".task-worktree.json") -Encoding UTF8

@"
# TASK_HANDOFF

- task_slug: $slug
- acceptance_mode: manual-required
- reason: 新建 detached worktree，尚未完成 verify / local-review。
- changed_files: 待 local-review 更新。
- verify_test_results: 待 verify 更新。
- local_review_results: 待 local-review 更新。
- optional_vs_code_review: ``code $createdPath``

## Dirty Overlap

- allow_dirty_overlap_from_head: $([bool]$AllowDirtyOverlapFromHead)
- overlap_paths: $($overlap -join ', ')
"@ | Set-Content -LiteralPath (Join-Path $createdPath "TASK_HANDOFF.md") -Encoding UTF8

Write-Output "created: $createdPath"
