<#
中文简介：
- 这个文件是什么：生成本地 diff 审查摘要、分级 acceptance gate，并更新 TASK_HANDOFF / .task-worktree.json。
- 什么时候读：提交或 PR 前，或完成前需要风险清单时。
- 约束什么：只读 Git diff；不调用云端 PR，不修改 staging。
#>

param(
  [switch]$Help,
  [switch]$DryRun
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
. "$PSScriptRoot\task-metadata.ps1"

function Show-Help {
  Write-Output "Usage: pwsh -NoProfile -File scripts/workflow/local-review.ps1 [-DryRun]"
}

if ($Help) { Show-Help; exit 0 }

$repoRoot = Resolve-WorkflowRepoRoot
$metadata = Read-TaskMetadata -RepoRoot $repoRoot -Require:(Test-IsTaskWorktreePath -Path $repoRoot)
$changed = @(Get-WorkflowChangedFiles -RepoRoot $repoRoot)
$scopes = @()
if ($metadata) {
  $scopes += @($metadata.target_paths)
  if ($metadata.PSObject.Properties.Name -contains "allowed_paths") { $scopes += @($metadata.allowed_paths) }
}

$unexpected = @()
$manualSignals = @()
foreach ($item in $changed) {
  if ($metadata -and -not (Test-PathWithinScopes -Path $item.Path -Scopes $scopes)) { $unexpected += $item.Path }
  if (Test-ManualRequiredPath -Path $item.Path) { $manualSignals += $item.Path }
}

$gate = "recommended-manual"
$reason = "存在未完全自动覆盖的改动。"
if ($manualSignals.Count -gt 0) {
  $gate = "manual-required"
  $reason = "触碰规则、workflow、保护资产、配置或高风险路径。"
} elseif ($unexpected.Count -gt 0) {
  $gate = "recommended-manual"
  $reason = "存在 target_paths / allowed_paths 之外的 unexpected changed files。"
} elseif ($metadata -and $metadata.verify_status -eq "passed" -and $changed.Count -gt 0) {
  $gate = "automated"
  $reason = "changed files 均在 allowlist 内，verify 通过，未发现 high-risk finding。"
}

Write-Output "repo_root: $repoRoot"
Write-Output "changed_files:"
if ($changed.Count -gt 0) { $changed | ForEach-Object { Write-Output "  $($_.Raw)" } } else { Write-Output "  none" }
Write-Output "unexpected_files:"
if ($unexpected.Count -gt 0) { $unexpected | ForEach-Object { Write-Output "  $_" } } else { Write-Output "  none" }
Write-Output "high_risk_findings:"
if ($manualSignals.Count -gt 0) { $manualSignals | ForEach-Object { Write-Output "  $_" } } else { Write-Output "  none" }
Write-Output "acceptance_gate: $gate"

if ($metadata -and -not $DryRun) {
  $metadata.acceptance_gate = $gate
  $metadata.manual_required = ($gate -eq "manual-required")
  $metadata.local_review_status = "completed"
  $metadata | Add-Member -NotePropertyName local_review_reason -NotePropertyValue $reason -Force
  $metadata.approved_changed_files = @($changed | ForEach-Object { $_.Path })
  $metadata | Add-Member -NotePropertyName unexpected_changed_files -NotePropertyValue @($unexpected) -Force
  $metadata | Add-Member -NotePropertyName high_risk_findings -NotePropertyValue @($manualSignals) -Force
  Write-TaskMetadata -Metadata $metadata -RepoRoot $repoRoot

  $handoff = Get-TaskHandoffPath -RepoRoot $repoRoot
  $changedLines = @($changed | ForEach-Object { "  - ``$($_.Path)`` [$($_.Status)]" })
  if ($changedLines.Count -eq 0) { $changedLines = @("  - none") }
  @"
# TASK_HANDOFF

- task_slug: $($metadata.task_slug)
- acceptance_mode: $gate
- reason: $reason
- changed_files:
$($changedLines -join [Environment]::NewLine)
- verify_test_results: $($metadata.verify_status)
- local_review_results: $($metadata.local_review_status)
- optional_vs_code_review: ``code $repoRoot``

## Risk Signals

- unexpected_files: $($unexpected.Count)
- high_risk_findings: $($manualSignals.Count)
- manual_accepted: $($metadata.manual_accepted)
"@ | Set-Content -LiteralPath $handoff -Encoding UTF8
}
