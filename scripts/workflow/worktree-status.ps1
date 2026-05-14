<#
中文简介：
- 这个文件是什么：输出当前仓库、task metadata 和 Git worktree 的只读状态。
- 什么时候读：任务开始、切换上下文或收尾前需要确认执行面时。
- 约束什么：只读检查；如果 task worktree 的 `.task-worktree.json` 缺字段，立即失败。
#>

param([switch]$Help)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
. "$PSScriptRoot\task-metadata.ps1"

function Show-Help {
  Write-Output "Usage: pwsh -NoProfile -File scripts/workflow/worktree-status.ps1"
}

if ($Help) { Show-Help; exit 0 }

$repoRoot = Resolve-WorkflowRepoRoot
$branch = git -C $repoRoot branch --show-current
$status = @(git -C $repoRoot status --porcelain)
$latest = git -C $repoRoot log -1 --oneline

Write-Output "repo_root: $repoRoot"
Write-Output "branch: $(if ($branch) { $branch } else { 'detached' })"
Write-Output "dirty: $(if ($status.Count -gt 0) { 'yes' } else { 'no' })"
Write-Output "latest_commit: $latest"

$metadata = Read-TaskMetadata -RepoRoot $repoRoot -Require:(Test-IsTaskWorktreePath -Path $repoRoot)
if ($metadata) {
  Write-Output "task_metadata:"
  Write-Output "  task_slug: $($metadata.task_slug)"
  Write-Output "  acceptance_gate: $($metadata.acceptance_gate)"
  Write-Output "  manual_required: $($metadata.manual_required)"
  Write-Output "  manual_accepted: $($metadata.manual_accepted)"
  Write-Output "  review_branch: $($metadata.review_branch)"
} elseif (Test-IsTaskWorktreePath -Path $repoRoot) {
  throw "task worktree 缺少 .task-worktree.json。"
}

Write-Output "status:"
if ($status.Count -gt 0) { $status | ForEach-Object { Write-Output "  $_" } } else { Write-Output "  clean" }
Write-Output "worktrees:"
git -C $repoRoot worktree list --porcelain
