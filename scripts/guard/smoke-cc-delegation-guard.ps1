Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$guardPath = Join-Path $repoRoot '.claude\hooks\cc-delegation-guard.ps1'

$cases = @(
  [pscustomobject]@{ Name = 'deny_edit_run'; Expected = 'deny'; Payload = @{ tool_name = 'Edit'; tool_input = @{ file_path = 'run/foo.py' } } },
  [pscustomobject]@{ Name = 'deny_write_run'; Expected = 'deny'; Payload = @{ tool_name = 'Write'; tool_input = @{ file_path = 'run/foo.py' } } },
  [pscustomobject]@{ Name = 'deny_bash_redirect_run'; Expected = 'deny'; Payload = @{ tool_name = 'Bash'; tool_input = @{ command = 'echo test > run/foo.py' } } },
  [pscustomobject]@{ Name = 'deny_bash_node_write_run'; Expected = 'deny'; Payload = @{ tool_name = 'Bash'; tool_input = @{ command = 'node -e "require(''fs'').writeFileSync(''run/foo.py'',''x'')"' } } },
  [pscustomobject]@{ Name = 'deny_bash_python_write_run'; Expected = 'deny'; Payload = @{ tool_name = 'Bash'; tool_input = @{ command = 'python -c "open(''run/foo.py'',''w'').write(''x'')"' } } },
  [pscustomobject]@{ Name = 'deny_guard_edit'; Expected = 'deny'; Payload = @{ tool_name = 'Edit'; tool_input = @{ file_path = '.claude/hooks/cc-delegation-guard.ps1' } } },
  [pscustomobject]@{ Name = 'deny_read_run'; Expected = 'deny'; Payload = @{ tool_name = 'Read'; tool_input = @{ file_path = 'run/foo.py' } } },
  [pscustomobject]@{ Name = 'deny_grep_run'; Expected = 'deny'; Payload = @{ tool_name = 'Grep'; tool_input = @{ path = 'run'; pattern = 'foo' } } },
  [pscustomobject]@{ Name = 'deny_ls_run'; Expected = 'deny'; Payload = @{ tool_name = 'LS'; tool_input = @{ path = 'run' } } },
  [pscustomobject]@{ Name = 'allow_companion_task_prompt_text'; Expected = 'allow'; Payload = @{ tool_name = 'Bash'; tool_input = @{ command = 'node C:/tools/codex-companion.mjs task "prompt contains run/ and > and <task> and --write"' } } },
  [pscustomobject]@{ Name = 'allow_companion_status'; Expected = 'allow'; Payload = @{ tool_name = 'Bash'; tool_input = @{ command = 'node C:/tools/codex-companion.mjs status' } } },
  [pscustomobject]@{ Name = 'allow_companion_cancel'; Expected = 'allow'; Payload = @{ tool_name = 'Bash'; tool_input = @{ command = 'node C:/tools/codex-companion.mjs cancel job-123' } } },
  [pscustomobject]@{ Name = 'allow_companion_resume_candidate'; Expected = 'allow'; Payload = @{ tool_name = 'Bash'; tool_input = @{ command = 'node C:/tools/codex-companion.mjs task-resume-candidate --json' } } },
  [pscustomobject]@{ Name = 'allow_codex_resume'; Expected = 'allow'; Payload = @{ tool_name = 'Bash'; tool_input = @{ command = 'codex resume session-123' } } },
  [pscustomobject]@{ Name = 'allow_codex_status'; Expected = 'allow'; Payload = @{ tool_name = 'Bash'; tool_input = @{ command = 'codex status' } } },
  [pscustomobject]@{ Name = 'allow_codex_review'; Expected = 'allow'; Payload = @{ tool_name = 'Bash'; tool_input = @{ command = 'codex review' } } },
  [pscustomobject]@{ Name = 'allow_git_status'; Expected = 'allow'; Payload = @{ tool_name = 'Bash'; tool_input = @{ command = 'git status --short' } } },
  [pscustomobject]@{ Name = 'allow_git_diff'; Expected = 'allow'; Payload = @{ tool_name = 'Bash'; tool_input = @{ command = 'git diff --stat' } } },
  [pscustomobject]@{ Name = 'allow_cc_work_read'; Expected = 'allow'; Payload = @{ tool_name = 'Read'; tool_input = @{ file_path = '.state/cc-work/README.md' } } },
  [pscustomobject]@{ Name = 'allow_claude_plans_read'; Expected = 'allow'; Payload = @{ tool_name = 'Read'; tool_input = @{ file_path = '.claude/plans/README.md' } } },
  [pscustomobject]@{ Name = 'ask_git_reset_hard'; Expected = 'ask'; Payload = @{ tool_name = 'Bash'; tool_input = @{ command = 'git reset --hard' } } },
  [pscustomobject]@{ Name = 'ask_git_clean_fd'; Expected = 'ask'; Payload = @{ tool_name = 'Bash'; tool_input = @{ command = 'git clean -fd' } } },
  [pscustomobject]@{ Name = 'ask_git_checkout_path'; Expected = 'ask'; Payload = @{ tool_name = 'Bash'; tool_input = @{ command = 'git checkout -- docs/workflows/cc-cx-delegation.md' } } },
  [pscustomobject]@{ Name = 'ask_git_commit'; Expected = 'ask'; Payload = @{ tool_name = 'Bash'; tool_input = @{ command = 'git commit -m "test"' } } },
  [pscustomobject]@{ Name = 'ask_git_push'; Expected = 'ask'; Payload = @{ tool_name = 'Bash'; tool_input = @{ command = 'git push origin HEAD' } } }
)

$results = foreach ($case in $cases) {
  $json = $case.Payload | ConvertTo-Json -Depth 8 -Compress
  $stdoutFile = [System.IO.Path]::GetTempFileName()
  $stderrFile = [System.IO.Path]::GetTempFileName()

  try {
    $json | & pwsh -NoProfile -ExecutionPolicy Bypass -File $guardPath 1> $stdoutFile 2> $stderrFile
    $exitCode = $LASTEXITCODE
    $stdout = [string](Get-Content -Raw $stdoutFile -ErrorAction SilentlyContinue)
    $stderr = [string](Get-Content -Raw $stderrFile -ErrorAction SilentlyContinue)
  } finally {
    Remove-Item -LiteralPath $stdoutFile, $stderrFile -Force -ErrorAction SilentlyContinue
  }

  $actual = 'allow'
  $reason = ([string]$stderr).Trim()

  if (-not [string]::IsNullOrWhiteSpace($stdout)) {
    try {
      $decision = $stdout | ConvertFrom-Json -ErrorAction Stop
      $actual = [string]$decision.hookSpecificOutput.permissionDecision
      $reason = [string]$decision.hookSpecificOutput.permissionDecisionReason
    } catch {
      $actual = 'invalid-json'
      $reason = ([string]$stdout).Trim()
    }
  }

  [pscustomobject]@{
    Name = $case.Name
    Expected = $case.Expected
    Actual = $actual
    Pass = ($case.Expected -eq $actual)
    ExitCode = $exitCode
    Reason = $reason
  }
}

$results | Format-Table Name, Expected, Actual, Pass -AutoSize

$failed = @($results | Where-Object { -not $_.Pass })
if ($failed.Count -gt 0) {
  ''
  'Failed cases:'
  $failed | Format-Table Name, Expected, Actual, ExitCode, Reason -AutoSize
  exit 1
}

''
'All guard smoke cases passed.'
