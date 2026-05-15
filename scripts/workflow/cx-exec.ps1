<#
中文简介：
- 这个文件是什么：Claude Code 调用 Codex 执行器的唯一入口。
- 什么时候读：CC 需要把明确任务交给 CX 在当前 task worktree 内执行时。
- 约束什么：使用 CC 独立 CODEX_HOME；不读 VS 插件目录；不做 stage、commit、push 或 PR。
#>

param(
  [string]$CodexHome = "C:\Users\apple\.codex-exec-cc",
  [string]$PromptFile = "CODEX_PROMPT.md",
  [string]$ResultFile = "CODEX_RESULT.md",
  [switch]$Help
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$script:CcCodexConfig = @'
model = "gpt-5.5"
model_provider = "codex-proxy"

[model_providers.codex-proxy]
name = "Codex Proxy"
base_url = "http://127.0.0.1:8080/v1"
env_key = "CODEX_PROXY_API_KEY"
wire_api = "responses"
'@

function Show-Help {
  Write-Output "Usage: pwsh -NoProfile -File scripts/workflow/cx-exec.ps1 [-CodexHome <path>] [-PromptFile CODEX_PROMPT.md] [-ResultFile CODEX_RESULT.md]"
}

function Get-CxExecRepoRoot {
  $root = git rev-parse --show-toplevel 2>$null
  if (-not $root) { throw "cx-exec.ps1 必须在 Git worktree 内运行。" }
  return (Resolve-Path -LiteralPath $root).Path
}

function Get-CxExecMetadataFiles {
  $roots = @("C:\Users\apple\worktrees", "C:\Users\apple\_worktrees")
  foreach ($root in $roots) {
    if (-not (Test-Path -LiteralPath $root)) { continue }
    Get-ChildItem -LiteralPath $root -Directory -Force -ErrorAction SilentlyContinue | ForEach-Object {
      $meta = Join-Path $_.FullName ".task-worktree.json"
      if (Test-Path -LiteralPath $meta) { Get-Item -LiteralPath $meta }
    }
  }
}

function Assert-CxExecActiveWorktree {
  param([string]$RepoRoot)

  $resolvedRoot = (Resolve-Path -LiteralPath $RepoRoot).Path
  $isTaskWorktree = $resolvedRoot.StartsWith("C:\Users\apple\worktrees", [System.StringComparison]::OrdinalIgnoreCase) -or
    $resolvedRoot.StartsWith("C:\Users\apple\_worktrees", [System.StringComparison]::OrdinalIgnoreCase)
  if (-not $isTaskWorktree) {
    throw "cx-exec.ps1 只能在 C:\Users\apple\worktrees 或 C:\Users\apple\_worktrees 下的 task worktree 内运行。"
  }

  $metadataFiles = @(Get-CxExecMetadataFiles)
  if ($metadataFiles.Count -ne 1) {
    throw "cx-exec.ps1 需要且只允许一个 active task worktree；当前发现 $($metadataFiles.Count) 个 metadata 文件。"
  }

  $metadata = Get-Content -LiteralPath $metadataFiles[0].FullName -Raw | ConvertFrom-Json
  if (-not ($metadata.PSObject.Properties.Name -contains "worktree_path")) {
    throw ".task-worktree.json 缺少 worktree_path，无法确认 active worktree。"
  }
  $metadataPath = (Resolve-Path -LiteralPath ([string]$metadata.worktree_path)).Path
  if ($metadataPath -ine $resolvedRoot) {
    throw "当前 cwd 不是登记的 active worktree：$metadataPath"
  }
}

function Ensure-CcCodexConfig {
  param([string]$CodexHome)

  if (-not (Test-Path -LiteralPath $CodexHome)) {
    New-Item -ItemType Directory -Path $CodexHome -Force | Out-Null
  }
  $configPath = Join-Path $CodexHome "config.toml"
  if (-not (Test-Path -LiteralPath $configPath)) {
    $script:CcCodexConfig | Set-Content -LiteralPath $configPath -Encoding UTF8
  }
  return $configPath
}

function Assert-CodexProxyApiKey {
  if ([string]::IsNullOrWhiteSpace($env:CODEX_PROXY_API_KEY)) {
    throw "CODEX_PROXY_API_KEY is missing. Set CODEX_PROXY_API_KEY in the current process before running scripts/workflow/cx-exec.ps1."
  }
}

function Invoke-CodexExec {
  param(
    [string]$RepoRoot,
    [string]$CodexHome,
    [string]$PromptText
  )

  $codex = Get-Command codex.exe -ErrorAction Stop | Select-Object -First 1
  $startInfo = [System.Diagnostics.ProcessStartInfo]::new()
  $startInfo.FileName = $codex.Source
  [void]$startInfo.ArgumentList.Add("exec")
  [void]$startInfo.ArgumentList.Add("-C")
  [void]$startInfo.ArgumentList.Add($RepoRoot)
  [void]$startInfo.ArgumentList.Add("--sandbox")
  [void]$startInfo.ArgumentList.Add("workspace-write")
  [void]$startInfo.ArgumentList.Add("-")
  $startInfo.RedirectStandardInput = $true
  $startInfo.RedirectStandardOutput = $true
  $startInfo.RedirectStandardError = $true
  $startInfo.UseShellExecute = $false
  $startInfo.Environment["CODEX_HOME"] = $CodexHome

  $process = [System.Diagnostics.Process]::new()
  $process.StartInfo = $startInfo
  [void]$process.Start()
  $process.StandardInput.Write($PromptText)
  $process.StandardInput.Close()
  $stdout = $process.StandardOutput.ReadToEnd()
  $stderr = $process.StandardError.ReadToEnd()
  $process.WaitForExit()

  return [pscustomobject]@{
    ExitCode = $process.ExitCode
    Stdout = $stdout
    Stderr = $stderr
  }
}

function Write-CodexResult {
  param(
    [string]$Path,
    [string]$RepoRoot,
    [string]$PromptPath,
    [string]$CodexHome,
    $Result
  )

  @"
# CODEX_RESULT

- repo_root: ``$RepoRoot``
- prompt_file: ``$PromptPath``
- codex_home: ``$CodexHome``
- command: ``codex exec -C "$RepoRoot" --sandbox workspace-write -``
- exit_code: $($Result.ExitCode)

## STDOUT

````text
$($Result.Stdout)
````

## STDERR

````text
$($Result.Stderr)
````
"@ | Set-Content -LiteralPath $Path -Encoding UTF8
}

function Invoke-CxExec {
  param(
    [string]$CodexHome,
    [string]$PromptFile,
    [string]$ResultFile
  )

  $repoRoot = Get-CxExecRepoRoot
  Assert-CxExecActiveWorktree -RepoRoot $repoRoot

  $promptPath = if ([System.IO.Path]::IsPathRooted($PromptFile)) { $PromptFile } else { Join-Path $repoRoot $PromptFile }
  $resultPath = if ([System.IO.Path]::IsPathRooted($ResultFile)) { $ResultFile } else { Join-Path $repoRoot $ResultFile }
  if (-not (Test-Path -LiteralPath $promptPath)) {
    throw "缺少 CODEX_PROMPT.md：$promptPath"
  }

  Assert-CodexProxyApiKey
  [void](Ensure-CcCodexConfig -CodexHome $CodexHome)

  $promptText = Get-Content -LiteralPath $promptPath -Raw
  $result = Invoke-CodexExec -RepoRoot $repoRoot -CodexHome $CodexHome -PromptText $promptText
  Write-CodexResult -Path $resultPath -RepoRoot $repoRoot -PromptPath $promptPath -CodexHome $CodexHome -Result $result
  if ($result.ExitCode -ne 0) {
    throw "codex exec failed with exit code $($result.ExitCode). See CODEX_RESULT.md for stdout/stderr."
  }
}

if ($Help) {
  Show-Help
  exit 0
}

if ($MyInvocation.InvocationName -ne ".") {
  Invoke-CxExec -CodexHome $CodexHome -PromptFile $PromptFile -ResultFile $ResultFile
}
