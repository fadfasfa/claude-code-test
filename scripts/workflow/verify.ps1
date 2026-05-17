<#
中文简介：
- 这个文件是什么：运行最小有效验证，并把结果写回 task metadata / handoff。
- 什么时候读：代码或工作流修改后声明完成前。
- 约束什么：不覆盖原始数据；无法判断时输出原因，不假装通过。
#>

param(
  [string]$Path = ".",
  [switch]$Help,
  [switch]$PlanOnly
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
. "$PSScriptRoot\task-metadata.ps1"

function Show-Help {
  Write-Output "Usage: pwsh -NoProfile -File scripts/workflow/verify.ps1 [-Path <subproject>] [-PlanOnly]"
}

function Test-CommandExists {
  param([string]$Name)
  return [bool](Get-Command $Name -ErrorAction SilentlyContinue)
}

function Invoke-VerificationCommand {
  param(
    [Parameter(Mandatory = $true)]
    [string]$Command,
    [Parameter(Mandatory = $true)]
    [string]$WorkingDirectory
  )

  $encodedCommand = [Convert]::ToBase64String([System.Text.Encoding]::Unicode.GetBytes($Command))
  $psi = [System.Diagnostics.ProcessStartInfo]::new()
  $psi.FileName = "pwsh"
  $psi.Arguments = "-NoProfile -EncodedCommand $encodedCommand"
  $psi.WorkingDirectory = $WorkingDirectory
  $psi.RedirectStandardOutput = $true
  $psi.RedirectStandardError = $true
  $psi.UseShellExecute = $false

  $process = [System.Diagnostics.Process]::Start($psi)
  $stdout = $process.StandardOutput.ReadToEnd()
  $stderr = $process.StandardError.ReadToEnd()
  $process.WaitForExit()

  foreach ($stream in @($stdout, $stderr)) {
    if ([string]::IsNullOrWhiteSpace($stream)) {
      continue
    }
    foreach ($line in ($stream -split "`r?`n")) {
      if (-not [string]::IsNullOrWhiteSpace($line)) {
        Write-Output $line
      }
    }
  }

  return $process.ExitCode
}

if ($Help) { Show-Help; exit 0 }

$repoRoot = Resolve-WorkflowRepoRoot
$target = Resolve-Path -LiteralPath $Path -ErrorAction Stop
$metadata = Read-TaskMetadata -RepoRoot $repoRoot -Require:(Test-IsTaskWorktreePath -Path $repoRoot)
$commands = @("git diff --check")

if (Test-Path -LiteralPath (Join-Path $target.Path "package.json")) {
  $pkg = Get-Content -LiteralPath (Join-Path $target.Path "package.json") -Raw
  if (Test-CommandExists "npm") {
    if ($pkg -match '"test"\s*:') { $commands += "npm test" }
    if ($pkg -match '"lint"\s*:') { $commands += "npm run lint" }
    if ($pkg -match '"build"\s*:') { $commands += "npm run build" }
  }
}
if ((Test-Path -LiteralPath (Join-Path $target.Path "pyproject.toml")) -or (Test-Path -LiteralPath (Join-Path $target.Path "requirements.txt"))) {
  if (Test-CommandExists "pytest") { $commands += "pytest" }
  if (Test-CommandExists "ruff") { $commands += "ruff check ." }
  if (Test-CommandExists "pyright") { $commands += "pyright" }
}

Write-Output "target: $($target.Path)"
Write-Output "planned_commands:"
$commands | ForEach-Object { Write-Output "  $_" }
if ($PlanOnly) {
  Write-Output "plan-only: 未执行验证命令。"
  exit 0
}

$results = @()
try {
  foreach ($cmd in $commands) {
    Write-Output "running: $cmd"
    $exitCode = Invoke-VerificationCommand -Command $cmd -WorkingDirectory $repoRoot
    if ($exitCode -ne 0) { throw "验证失败：$cmd (exit $exitCode)" }
    $results += "PASS $cmd"
  }
  if ($metadata) {
    $metadata.verify_status = "passed"
    $metadata | Add-Member -NotePropertyName verify_results -NotePropertyValue @($results) -Force
    Write-TaskMetadata -Metadata $metadata -RepoRoot $repoRoot
  }
  Write-Output "verify_status: passed"
} catch {
  if ($metadata) {
    $metadata.verify_status = "failed"
    $metadata | Add-Member -NotePropertyName verify_results -NotePropertyValue @($results + "FAIL $($_.Exception.Message)") -Force
    Write-TaskMetadata -Metadata $metadata -RepoRoot $repoRoot
  }
  throw
}
