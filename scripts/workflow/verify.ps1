<#
中文简介：
- 这个文件是什么：根据当前子项目线索选择最小验证命令。
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

function Show-Help {
  Write-Output "Usage: pwsh -NoProfile -File scripts/workflow/verify.ps1 [-Path <subproject>] [-PlanOnly]"
  Write-Output "Detects Python, frontend, crawler, or data project signals and runs or suggests minimal checks."
}

function Resolve-TargetPath {
  param([string]$InputPath)
  $resolved = Resolve-Path -LiteralPath $InputPath -ErrorAction Stop
  return $resolved.Path
}

function Test-CommandExists {
  param([string]$Name)
  return [bool](Get-Command $Name -ErrorAction SilentlyContinue)
}

if ($Help) {
  Show-Help
  exit 0
}

$target = Resolve-TargetPath $Path
Write-Output "target: $target"

$commands = @()
if (Test-Path -LiteralPath (Join-Path $target "package.json")) {
  if (Test-CommandExists "npm") {
    $pkg = Get-Content -LiteralPath (Join-Path $target "package.json") -Raw
    if ($pkg -match '"test"\s*:') { $commands += "npm test" }
    if ($pkg -match '"lint"\s*:') { $commands += "npm run lint" }
    if ($pkg -match '"build"\s*:') { $commands += "npm run build" }
  } else {
    Write-Output "npm 不可用，无法运行 frontend 验证。"
  }
}

if ((Test-Path -LiteralPath (Join-Path $target "pyproject.toml")) -or (Test-Path -LiteralPath (Join-Path $target "requirements.txt"))) {
  if (Test-CommandExists "pytest") { $commands += "pytest" }
  if (Test-CommandExists "ruff") { $commands += "ruff check ." }
  if (Test-CommandExists "pyright") { $commands += "pyright" }
}

if ((Test-Path -LiteralPath (Join-Path $target "scrapy.cfg")) -or ($target -match 'heybox|crawler|spider')) {
  Write-Output "检测到爬虫线索：优先 dry-run、sample 或小范围请求；不得覆盖原始数据。"
}

if ($target -match 'data|QuantProject|run') {
  Write-Output "检测到数据项目线索：优先小样本验证；不得覆盖原始数据。"
}

if ($commands.Count -eq 0) {
  Write-Output "无法自动判断可运行验证命令；请查看子项目 README、pyproject.toml、package.json 或现有 scripts。"
  exit 2
}

Write-Output "planned_commands:"
$commands | ForEach-Object { Write-Output "  $_" }

if ($PlanOnly) {
  Write-Output "plan-only: 未执行验证命令。"
  exit 0
}

foreach ($cmd in $commands) {
  Write-Output "running: $cmd"
  pwsh -NoProfile -Command $cmd
  if ($LASTEXITCODE -ne 0) { throw "验证失败：$cmd" }
}
