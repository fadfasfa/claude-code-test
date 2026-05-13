<#
中文简介：
- 这个文件是什么：发布准备入口，默认只做 dry-run。
- 什么时候读：用户明确要求提交、推送或创建 PR 前。
- 约束什么：commit/push/PR 必须显式授权；push/PR 还需要二次确认字符串。
#>

param(
  [string]$Message,
  [string]$Remote = "origin",
  [switch]$AllowCommit,
  [switch]$AllowPush,
  [switch]$AllowPr,
  [string]$ConfirmPublish,
  [switch]$DryRun,
  [switch]$Help
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Show-Help {
  Write-Output "Usage: pwsh -NoProfile -File scripts/workflow/finalize-pr.ps1 [-AllowCommit -Message <msg>] [-AllowPush -AllowPr -ConfirmPublish CONFIRM-PUBLISH]"
  Write-Output "Default is dry-run and performs no git add, commit, push, or PR."
}

if ($Help) {
  Show-Help
  exit 0
}

if ($DryRun -and ($AllowCommit -or $AllowPush -or $AllowPr)) {
  throw "-DryRun 不能与发布动作参数同时使用。"
}

Write-Output "dry-run summary:"
git status --short
git diff --stat

if ($DryRun -or (-not $AllowCommit -and -not $AllowPush -and -not $AllowPr)) {
  Write-Output "dry-run: no publish action requested."
  exit 0
}

if ($AllowCommit) {
  if ([string]::IsNullOrWhiteSpace($Message)) { throw "commit 需要 -Message。" }
  throw "停止：本脚本不会自动 git add。请在用户明确授权并人工确认 staged diff 后再扩展执行。"
}

if ($AllowPush -or $AllowPr) {
  if ($ConfirmPublish -ne "CONFIRM-PUBLISH") {
    throw "push / PR 需要二次确认：-ConfirmPublish CONFIRM-PUBLISH"
  }
  throw "停止：push / PR 已请求但本仓默认不自动发布。请人工确认远端、分支和 PR 内容。"
}
