<#
中文简介：
- 这个文件是什么：检查并准备唯一 active worktree。
- 什么时候读：准备进入新任务执行面时。
- 约束什么：默认 dry-run；只有传入 -Apply 才会创建 worktree。
#>

param(
  [string]$Topic,

  [string]$Type = "task",

  [string]$Base = "main",

  [string]$Root = "C:\Users\apple\worktrees",

  [switch]$DryRun,

  [switch]$Apply,

  [switch]$Help
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Show-Help {
  Write-Output "Usage: pwsh -NoProfile -File scripts/workflow/worktree-start.ps1 -Topic <name> [-Apply]"
  Write-Output "Default is dry-run. -Apply creates a codex/<type>/<topic> worktree when checks pass."
}

function Resolve-RepoRoot {
  $root = git rev-parse --show-toplevel 2>$null
  if (-not $root) { throw "未在 Git 仓库内，无法解析 repo root。" }
  return (Resolve-Path -LiteralPath $root).Path
}

function Get-Slug {
  param([string]$InputText)
  $slug = ($InputText.ToLower() -replace '[^a-z0-9]+', '-').Trim('-')
  if ([string]::IsNullOrWhiteSpace($slug)) { throw "Topic 经过 slug 化后为空。" }
  return $slug
}

if ($Help) {
  Show-Help
  exit 0
}

if ([string]::IsNullOrWhiteSpace($Topic)) {
  Show-Help
  throw "必须提供 -Topic，除非只使用 -Help。"
}

if ($DryRun -and $Apply) {
  throw "-DryRun 与 -Apply 不能同时使用。"
}

$repoRoot = Resolve-RepoRoot
$dirty = @(git -C $repoRoot status --porcelain)
if ($dirty.Count -gt 0) {
  Write-Output "主仓存在未提交改动，创建前请确认这些改动属于当前任务或已被隔离："
  $dirty | ForEach-Object { Write-Output "  $_" }
}

$active = @(git -C $repoRoot worktree list --porcelain | Select-String -Pattern '^worktree\s+C:/Users/apple/worktrees|^worktree\s+C:\\Users\\apple\\worktrees')
if ($active.Count -gt 0) {
  Write-Output "检测到 worktrees root 下已有 worktree；默认不并发开启多个任务分支。"
  $active | ForEach-Object { Write-Output "  $($_.Line)" }
  exit 1
}

$slug = Get-Slug $Topic
$branch = "codex/$Type/$slug"
$path = Join-Path $Root "$Type-$slug"

Write-Output "repo_root: $repoRoot"
Write-Output "target_path: $path"
Write-Output "branch: $branch"
Write-Output "base: $Base"

if ($DryRun -or -not $Apply) {
  Write-Output "dry-run: 未创建 worktree。传入 -Apply 才会执行 git worktree add。"
  exit 0
}

if (Test-Path -LiteralPath $path) { throw "目标 worktree 路径已存在：$path" }
New-Item -ItemType Directory -Path $Root -Force | Out-Null
git -C $repoRoot worktree add $path -b $branch $Base
if ($LASTEXITCODE -ne 0) { throw "git worktree add 失败。" }
Write-Output "created: $path"
