<#
中文简介：
- 这个文件是什么：CC -> Codex 的真实执行器，供根目录 cx-exec.ps1 转发调用。
- 什么时候读：Claude Code 需要让 Codex 在本仓读代码、写代码或跑命令时。
- 约束什么：固定使用用户级 .codex-exec 与 C# wrapper；运行结果只写入 .state/workflow/tasks/<task_id>/，不生成计划或 Markdown 报告。
- 输入输出：读取当前 Git 状态和任务参数；写 result.json、codex.log、codex.err.log 到 task 目录。
- 依赖路径：C:\Users\apple\.codex-exec、C:\Users\apple\codex-maintenance\codex-exec-wrapper.exe 和本仓 .state/workflow/。
- 修改行为：DryRun 只写结构化 dry-run 结果；非 DryRun 才做 proxy / wrapper preflight 并调用 Codex。
- 失败行为：环境、认证、wrapper 或 timeout 失败都会写入 result.json 并返回非零退出码。
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

$script:CodexHome = "C:\Users\apple\.codex-exec"
$script:WrapperExe = "C:\Users\apple\codex-maintenance\codex-exec-wrapper.exe"
$script:ProxyHealthUrl = "http://127.0.0.1:8080/health"
$script:CodexTimeoutSec = 120

function Get-CxRepoRoot {
  $root = git rev-parse --show-toplevel 2>$null
  if (-not $root) {
    throw "cx-exec.ps1 must run inside a Git worktree."
  }
  return (Resolve-Path -LiteralPath $root).Path
}

function New-CxTaskId {
  param([string]$Description)

  $stamp = Get-Date -Format "yyyyMMdd-HHmmss"
  $slug = ($Description.ToLowerInvariant() -replace '[^a-z0-9]+', '-').Trim('-')
  if ([string]::IsNullOrWhiteSpace($slug)) {
    $slug = "task"
  }
  if ($slug.Length -gt 40) {
    $slug = $slug.Substring(0, 40).Trim('-')
  }
  return "$stamp-$slug"
}

function ConvertTo-SafeTaskId {
  param([string]$Value)

  if ([string]::IsNullOrWhiteSpace($Value)) {
    return (New-CxTaskId -Description $TaskDescription)
  }
  $safe = ($Value -replace '[^A-Za-z0-9._-]+', '-').Trim('.-_')
  if ([string]::IsNullOrWhiteSpace($safe)) {
    return (New-CxTaskId -Description $TaskDescription)
  }
  return $safe
}

function Get-GitStatusText {
  $text = git status --short --untracked-files=all 2>&1
  if ($LASTEXITCODE -ne 0) {
    return [string]$text
  }
  return (($text | Out-String).TrimEnd())
}

function Get-ChangedFilesFromStatus {
  param([string]$StatusText)

  $result = @{
    modified = @()
    created = @()
    deleted = @()
  }
  if ([string]::IsNullOrWhiteSpace($StatusText)) {
    return $result
  }

  foreach ($line in ($StatusText -split "`r?`n")) {
    if ($line.Length -lt 4) { continue }
    $status = $line.Substring(0, 2)
    $path = $line.Substring(3).Trim()
    if ($status -eq "??" -or $status.Contains("A")) {
      $result.created += $path
    } elseif ($status.Contains("D")) {
      $result.deleted += $path
    } else {
      $result.modified += $path
    }
  }
  return $result
}

function Get-TailText {
  param(
    [string]$Text,
    [int]$MaxChars = 4000
  )

  if ([string]::IsNullOrEmpty($Text)) {
    return ""
  }
  if ($Text.Length -le $MaxChars) {
    return $Text
  }
  return $Text.Substring($Text.Length - $MaxChars)
}

function Classify-CxError {
  param([string]$Text)

  if ($Text -match '(?i)401|403|Unauthorized|Missing bearer') {
    return @{ type = "auth"; retry = $false }
  }
  if ($Text -match '(?i)proxy unreachable|config missing|wrapper missing|CODEX_PROXY_API_KEY missing|connection refused|health') {
    return @{ type = "env"; retry = $false }
  }
  if (-not [string]::IsNullOrWhiteSpace($Text)) {
    return @{ type = "code"; retry = $true }
  }
  return @{ type = "unknown"; retry = $false }
}

function Test-CxPreflight {
  $errors = @()
  $healthSummary = $null
  $apiKeyStatus = if ([string]::IsNullOrWhiteSpace($env:CODEX_PROXY_API_KEY)) { "missing" } else { "set" }

  if (-not (Test-Path -LiteralPath $script:WrapperExe)) {
    $errors += "wrapper missing: $script:WrapperExe"
  }

  $configPath = Join-Path $script:CodexHome "config.toml"
  if (-not (Test-Path -LiteralPath $configPath)) {
    $errors += "config missing: $configPath"
  }

  if ($apiKeyStatus -eq "missing") {
    $errors += "CODEX_PROXY_API_KEY missing"
  }

  try {
    $health = Invoke-RestMethod -Uri $script:ProxyHealthUrl -Method Get -TimeoutSec 5 -ErrorAction Stop
    $healthSummary = [pscustomobject]@{
      status = $health.status
      authenticated = $health.authenticated
      pool_total = $health.pool.total
      pool_active = $health.pool.active
    }
    if ($health.status -ne "ok" -or $health.authenticated -ne $true) {
      $errors += "proxy health not ready: status=$($health.status), authenticated=$($health.authenticated)"
    }
  } catch {
    $errors += "proxy unreachable: $($_.Exception.Message)"
  }

  return [pscustomobject]@{
    ok = ($errors.Count -eq 0)
    errors = $errors
    api_key = $apiKeyStatus
    health = $healthSummary
    config_path = $configPath
    wrapper = $script:WrapperExe
    codex_home = $script:CodexHome
  }
}

function Write-CxResultJson {
  param(
    [string]$Path,
    [object]$Payload
  )

  $Payload | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $Path -Encoding UTF8
}

function New-CxResultPayload {
  param(
    [string]$TaskId,
    [string]$Status,
    [string]$Summary,
    [string]$PreGitStatus,
    [string]$PostGitStatus,
    [string[]]$CommandsRun,
    [Nullable[int]]$ExitCode,
    [Nullable[double]]$DurationSec,
    [object]$ErrorObject,
    [bool]$RetryAdvised,
    [string]$NextSuggestion
  )

  $changed = Get-ChangedFilesFromStatus -StatusText $PostGitStatus
  return [ordered]@{
    task_id = $TaskId
    status = $Status
    attempts = 1
    summary = $Summary
    changes = [ordered]@{
      pre_git_status = $PreGitStatus
      post_git_status = $PostGitStatus
      files_modified = @($changed.modified)
      files_created = @($changed.created)
      files_deleted = @($changed.deleted)
    }
    verification = [ordered]@{
      commands_run = @($CommandsRun)
      tests_passed = $null
      exit_code = $ExitCode
      duration_sec = $DurationSec
    }
    error = $ErrorObject
    retry_advised = $RetryAdvised
    next_suggestion = $NextSuggestion
  }
}

$repoRoot = Get-CxRepoRoot
$resolvedTaskId = ConvertTo-SafeTaskId -Value $TaskId
$workflowRoot = Join-Path $repoRoot ".state\workflow"
$taskRoot = Join-Path (Join-Path $workflowRoot "tasks") $resolvedTaskId
$resultPath = Join-Path $taskRoot "result.json"
$stdoutPath = Join-Path $taskRoot "codex.log"
$stderrPath = Join-Path $taskRoot "codex.err.log"

New-Item -ItemType Directory -Path $taskRoot -Force | Out-Null

$preGitStatus = Get-GitStatusText
$commands = @(
  "git status --short --untracked-files=all"
)

if ($DryRun) {
  $dryText = "DryRun only. Codex preflight and invocation were skipped. task_id=$resolvedTaskId; profile=$Profile; CODEX_HOME=$script:CodexHome."
  $dryText | Set-Content -LiteralPath $stdoutPath -Encoding UTF8
  "" | Set-Content -LiteralPath $stderrPath -Encoding UTF8
  $postGitStatus = Get-GitStatusText
  $payload = New-CxResultPayload -TaskId $resolvedTaskId -Status "success" -Summary "DryRun completed; Codex was not invoked." -PreGitStatus $preGitStatus -PostGitStatus $postGitStatus -CommandsRun ($commands + @("dry-run: skipped codex preflight and invocation")) -ExitCode 0 -DurationSec 0 -ErrorObject $null -RetryAdvised $false -NextSuggestion "Run without -DryRun when CC is ready to delegate the task to Codex."
  Write-CxResultJson -Path $resultPath -Payload $payload
  Write-Output "result: $resultPath"
  exit 0
}

$commands = $commands + @(
  "Invoke-RestMethod $script:ProxyHealthUrl",
  "$script:WrapperExe exec --profile $Profile -C $repoRoot --sandbox workspace-write <task>"
)
$preflight = Test-CxPreflight

if (-not $preflight.ok) {
  $message = ($preflight.errors -join "; ")
  $classification = Classify-CxError -Text $message
  "" | Set-Content -LiteralPath $stdoutPath -Encoding UTF8
  $message | Set-Content -LiteralPath $stderrPath -Encoding UTF8
  $postGitStatus = Get-GitStatusText
  $payload = New-CxResultPayload -TaskId $resolvedTaskId -Status "failed" -Summary "Preflight failed before invoking Codex." -PreGitStatus $preGitStatus -PostGitStatus $postGitStatus -CommandsRun $commands -ExitCode 1 -DurationSec 0 -ErrorObject ([ordered]@{ type = $classification.type; message = $message; stderr_tail = $message }) -RetryAdvised $classification.retry -NextSuggestion "Fix the reported environment/auth prerequisite, then rerun the same cx task."
  Write-CxResultJson -Path $resultPath -Payload $payload
  Write-Output "result: $resultPath"
  exit 1
}

$stopwatch = [System.Diagnostics.Stopwatch]::StartNew()
$startInfo = [System.Diagnostics.ProcessStartInfo]::new()
$startInfo.FileName = $script:WrapperExe
[void]$startInfo.ArgumentList.Add("exec")
[void]$startInfo.ArgumentList.Add("--profile")
[void]$startInfo.ArgumentList.Add($Profile)
[void]$startInfo.ArgumentList.Add("-C")
[void]$startInfo.ArgumentList.Add($repoRoot)
[void]$startInfo.ArgumentList.Add("--sandbox")
[void]$startInfo.ArgumentList.Add("workspace-write")
[void]$startInfo.ArgumentList.Add($TaskDescription)
$startInfo.RedirectStandardOutput = $true
$startInfo.RedirectStandardError = $true
$startInfo.UseShellExecute = $false
$startInfo.Environment["CODEX_HOME"] = $script:CodexHome

$process = [System.Diagnostics.Process]::new()
$process.StartInfo = $startInfo
[void]$process.Start()
$stdoutTask = $process.StandardOutput.ReadToEndAsync()
$stderrTask = $process.StandardError.ReadToEndAsync()
$completed = $process.WaitForExit($script:CodexTimeoutSec * 1000)
if (-not $completed) {
  try {
    $process.Kill($true)
  } catch {
    $process.Kill()
  }
  $process.WaitForExit()
}
$stdout = $stdoutTask.GetAwaiter().GetResult()
$stderr = $stderrTask.GetAwaiter().GetResult()
$stopwatch.Stop()

$stdout | Set-Content -LiteralPath $stdoutPath -Encoding UTF8
$stderr | Set-Content -LiteralPath $stderrPath -Encoding UTF8
$postStatus = Get-GitStatusText

if (-not $completed) {
  $timeoutMessage = "Codex execution timed out after $script:CodexTimeoutSec seconds."
  $combinedTimeout = (($timeoutMessage, $stderr, $stdout) -join "`n")
  $classification = Classify-CxError -Text $combinedTimeout
  $payload = New-CxResultPayload -TaskId $resolvedTaskId -Status "failed" -Summary $timeoutMessage -PreGitStatus $preGitStatus -PostGitStatus $postStatus -CommandsRun $commands -ExitCode 124 -DurationSec ([math]::Round($stopwatch.Elapsed.TotalSeconds, 3)) -ErrorObject ([ordered]@{ type = "unknown"; message = $timeoutMessage; stderr_tail = (Get-TailText -Text $combinedTimeout) }) -RetryAdvised $false -NextSuggestion "Inspect codex.log and codex.err.log; retry only after confirming this was a transient hang rather than an auth/config issue."
  Write-CxResultJson -Path $resultPath -Payload $payload
  Write-Output "result: $resultPath"
  exit 124
}

if ($process.ExitCode -eq 0) {
  $payload = New-CxResultPayload -TaskId $resolvedTaskId -Status "success" -Summary "Codex completed successfully." -PreGitStatus $preGitStatus -PostGitStatus $postStatus -CommandsRun $commands -ExitCode $process.ExitCode -DurationSec ([math]::Round($stopwatch.Elapsed.TotalSeconds, 3)) -ErrorObject $null -RetryAdvised $false -NextSuggestion "CC should inspect result.json and codex.log, then decide acceptance or follow-up."
  Write-CxResultJson -Path $resultPath -Payload $payload
  Write-Output "result: $resultPath"
  exit 0
}

$combinedError = (($stderr, $stdout) -join "`n")
$classification = Classify-CxError -Text $combinedError
$payload = New-CxResultPayload -TaskId $resolvedTaskId -Status "failed" -Summary "Codex returned a non-zero exit code." -PreGitStatus $preGitStatus -PostGitStatus $postStatus -CommandsRun $commands -ExitCode $process.ExitCode -DurationSec ([math]::Round($stopwatch.Elapsed.TotalSeconds, 3)) -ErrorObject ([ordered]@{ type = $classification.type; message = "Codex exited with code $($process.ExitCode)."; stderr_tail = (Get-TailText -Text $combinedError) }) -RetryAdvised $classification.retry -NextSuggestion "Review codex.err.log and result.json before retrying."
Write-CxResultJson -Path $resultPath -Payload $payload
Write-Output "result: $resultPath"
exit $process.ExitCode
