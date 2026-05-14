<#
中文简介：
- 这个文件是什么：发布准备入口，默认只做 dry-run。
- 什么时候读：用户明确授权创建 review branch、commit、push 或 PR 前。
- 约束什么：每一级授权独立；commit 只能 stage metadata 记录的 approved changed files，禁止 git add .。
#>

param(
  [string]$Message,
  [string]$Area = "task",
  [string]$Remote = "origin",
  [switch]$CreateReviewBranch,
  [switch]$ReuseReviewBranch,
  [switch]$RecreateReviewBranch,
  [switch]$AllowCommit,
  [switch]$AllowPush,
  [switch]$AllowPR,
  [string]$ConfirmPublish,
  [switch]$DryRun,
  [switch]$Help
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
. "$PSScriptRoot\task-metadata.ps1"

function Show-Help {
  Write-Output "Usage: pwsh -NoProfile -File scripts/workflow/finalize-pr.ps1 [-CreateReviewBranch] [-AllowCommit -Message <msg>] [-AllowPush|-AllowPR -ConfirmPublish CONFIRM-PUBLISH]"
  Write-Output "Default is dry-run. Authorizations are independent and never imply the next level."
}

function Test-BranchExists {
  param(
    [string]$RepoRoot,
    [string]$Branch
  )
  git -C $RepoRoot show-ref --verify --quiet "refs/heads/$Branch"
  return ($LASTEXITCODE -eq 0)
}

if ($Help) { Show-Help; exit 0 }

$repoRoot = Resolve-WorkflowRepoRoot
$metadata = Read-TaskMetadata -RepoRoot $repoRoot -Require
$changed = @(Get-WorkflowChangedFiles -RepoRoot $repoRoot)
$approved = @($metadata.approved_changed_files | ForEach-Object { ([string]$_ -replace '\\', '/') })
$changedPaths = @($changed | ForEach-Object { $_.Path })
$unregistered = @($changedPaths | Where-Object { $approved -notcontains $_ })
$reviewBranch = if ($metadata.review_branch) { [string]$metadata.review_branch } else { "codex/$Area/$($metadata.task_slug)" }

Write-Output "repo_root: $repoRoot"
Write-Output "acceptance_gate: $($metadata.acceptance_gate)"
Write-Output "manual_required: $($metadata.manual_required)"
Write-Output "manual_accepted: $($metadata.manual_accepted)"
Write-Output "review_branch: $reviewBranch"
Write-Output "changed_files:"
if ($changed.Count -gt 0) { $changed | ForEach-Object { Write-Output "  $($_.Raw)" } } else { Write-Output "  clean" }
Write-Output "unregistered_files:"
if ($unregistered.Count -gt 0) { $unregistered | ForEach-Object { Write-Output "  $_" } } else { Write-Output "  none" }

if ($metadata.acceptance_gate -eq "manual-required" -and -not [bool]$metadata.manual_accepted -and ($AllowCommit -or $CreateReviewBranch)) {
  throw "acceptance_gate=manual-required 且 manual_accepted=false，禁止进入 review branch / commit。"
}

if ($AllowCommit -and $unregistered.Count -gt 0) {
  throw "存在未登记 modified/untracked files，停止 staging；请重新运行 local-review.ps1。"
}

if ($DryRun -or (-not $CreateReviewBranch -and -not $AllowCommit -and -not $AllowPush -and -not $AllowPR)) {
  Write-Output "dry-run: 未执行 branch、stage、commit、push 或 PR。"
  exit 0
}

if ($CreateReviewBranch) {
  $exists = Test-BranchExists -RepoRoot $repoRoot -Branch $reviewBranch
  if ($exists -and -not $ReuseReviewBranch -and -not $RecreateReviewBranch) {
    throw "review branch 已存在：$reviewBranch。默认不覆盖；如需复用传 -ReuseReviewBranch，如需重建传 -RecreateReviewBranch。"
  }
  if ($exists -and $RecreateReviewBranch) {
    git -C $repoRoot branch -D $reviewBranch
    if ($LASTEXITCODE -ne 0) { throw "删除已有 review branch 失败：$reviewBranch" }
    git -C $repoRoot switch -c $reviewBranch
  } elseif ($exists -and $ReuseReviewBranch) {
    git -C $repoRoot switch $reviewBranch
  } else {
    git -C $repoRoot switch -c $reviewBranch
  }
  if ($LASTEXITCODE -ne 0) { throw "切换/创建 review branch 失败：$reviewBranch" }
  $metadata.review_branch = $reviewBranch
  Write-TaskMetadata -Metadata $metadata -RepoRoot $repoRoot
}

if ($AllowCommit) {
  if ([string]::IsNullOrWhiteSpace($Message)) { throw "commit 需要 -Message。" }
  $toStage = @($approved | Where-Object { -not [string]::IsNullOrWhiteSpace($_) })
  if ($toStage.Count -eq 0) { throw "approved_changed_files 为空，禁止 commit。" }
  foreach ($path in $toStage) {
    git -C $repoRoot add -- $path
    if ($LASTEXITCODE -ne 0) { throw "stage 失败：$path" }
  }
  git -C $repoRoot commit -m $Message
  if ($LASTEXITCODE -ne 0) { throw "commit 失败。" }
}

if ($AllowPush -or $AllowPR) {
  if ($ConfirmPublish -ne "CONFIRM-PUBLISH") { throw "push / PR 需要二次确认：-ConfirmPublish CONFIRM-PUBLISH" }
}
if ($AllowPush) {
  git -C $repoRoot push $Remote HEAD
  if ($LASTEXITCODE -ne 0) { throw "push 失败。" }
}
if ($AllowPR) {
  throw "PR 创建未自动实现；请在明确授权后使用 GitHub 工具或 gh，并保持 PR 与 merge 分离。"
}
