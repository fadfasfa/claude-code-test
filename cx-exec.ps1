#!/usr/bin/env pwsh
<#
中文简介：
- 这个文件是什么：Claude Code 调用 Codex 的根目录兼容入口。
- 什么时候读：CC 从仓库根目录发起 cx 任务时。
- 约束什么：只转发到 scripts/workflow/cx-exec.ps1；不在根目录写运行产物。
- 参数：TaskId 可选，TaskDescription 必填，Profile 选择执行配置，DryRun 只验证路径转发。
- 失败行为：找不到真实 executor 时立即抛错；不回退到其他 Codex 入口。
#>

param(
  [string]$TaskId,
  [Parameter(Mandatory = $true)]
  [string]$TaskDescription,
  [ValidateSet("design", "implement", "review", "lint", "full-access")]
  [string]$Profile = "implement",
  [switch]$DryRun
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$executor = Join-Path $PSScriptRoot "scripts\workflow\cx-exec.ps1"
if (-not (Test-Path -LiteralPath $executor)) {
  throw "Missing workflow executor: $executor"
}

$forward = @{
  TaskDescription = $TaskDescription
  Profile = $Profile
}

if (-not [string]::IsNullOrWhiteSpace($TaskId)) {
  $forward.TaskId = $TaskId
}

if ($DryRun) {
  $forward.DryRun = $true
}

& $executor @forward
