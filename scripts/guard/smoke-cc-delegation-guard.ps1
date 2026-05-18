Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$guardPath = Join-Path $repoRoot '.claude\hooks\cc-delegation-guard.ps1'

# Guard smoke 必须可重复、无仓库运行态污染：状态文件使用临时路径，debug log 默认关闭。
$codexResearcherDelegation = @{ source = 'codex-thread'; role = 'researcher'; phase = 'explore' }

function New-BashPayload {
  param(
    [Parameter(Mandatory = $true)][string]$Command,
    [AllowNull()][hashtable]$Delegation = $null
  )

  $payload = @{
    tool_name = 'Bash'
    tool_input = @{
      command = $Command
    }
  }

  if ($null -ne $Delegation) {
    $payload.codex_delegation = $Delegation
  }

  return $payload
}

function New-Case {
  param(
    [Parameter(Mandatory = $true)][string]$Name,
    [Parameter(Mandatory = $true)][string]$Expected,
    [AllowNull()][object]$Payload = $null,
    [AllowNull()][string]$Raw = $null,
    [AllowNull()][hashtable]$State = $null,
    [AllowNull()][string]$RuleId = $null
  )

  [pscustomobject]@{
    Name = $Name
    Expected = $Expected
    Payload = $Payload
    Raw = $Raw
    State = $State
    RuleId = $RuleId
  }
}

$cases = @(
  (New-Case -Name 'allow_plugin_companion_task_prompt_paths' -Expected 'allow' -Payload (New-BashPayload -Command 'node C:/tools/codex-companion.mjs task "inspect run/** and QuantProject/** in delegated prompt only"')),
  (New-Case -Name 'allow_plugin_companion_status' -Expected 'allow' -Payload (New-BashPayload -Command 'node C:/tools/codex-companion.mjs status')),
  (New-Case -Name 'allow_plugin_companion_cancel' -Expected 'allow' -Payload (New-BashPayload -Command 'node C:/tools/codex-companion.mjs cancel job-123')),
  (New-Case -Name 'allow_plugin_companion_resume' -Expected 'allow' -Payload (New-BashPayload -Command 'node C:/tools/codex-companion.mjs resume job-123')),
  (New-Case -Name 'allow_plugin_companion_review' -Expected 'allow' -Payload (New-BashPayload -Command 'node "C:/Users/apple/.claude/plugins/cache/openai-codex/codex/1.0.4/scripts/codex-companion.mjs" review "--wait"')),
  (New-Case -Name 'allow_plugin_companion_resume_candidate' -Expected 'allow' -Payload (New-BashPayload -Command 'node C:/tools/codex-companion.mjs task-resume-candidate --json')),
  (New-Case -Name 'deny_cx_degraded_new_task' -Expected 'deny' -RuleId 'CX_DEGRADED_NO_NEW_CODEX_TASK' -State @{ state = 'CX_DEGRADED'; reason = 'smoke' } -Payload (New-BashPayload -Command 'node C:/tools/codex-companion.mjs task "try again"')),
  (New-Case -Name 'deny_cx_degraded_resume' -Expected 'deny' -RuleId 'CX_DEGRADED_NO_NEW_CODEX_TASK' -State @{ state = 'CX_DEGRADED'; reason = 'smoke' } -Payload (New-BashPayload -Command 'node C:/tools/codex-companion.mjs resume job-123')),
  (New-Case -Name 'deny_cx_degraded_review_real_command' -Expected 'deny' -RuleId 'CX_DEGRADED_NO_NEW_CODEX_TASK' -State @{ state = 'CX_DEGRADED'; reason = 'smoke' } -Payload (New-BashPayload -Command 'node "C:/Users/apple/.claude/plugins/cache/openai-codex/codex/1.0.4/scripts/codex-companion.mjs" review "--wait"')),
  (New-Case -Name 'allow_cx_degraded_status' -Expected 'allow' -State @{ state = 'CX_DEGRADED'; reason = 'smoke' } -Payload (New-BashPayload -Command 'node C:/tools/codex-companion.mjs status')),
  (New-Case -Name 'allow_cx_degraded_cancel' -Expected 'allow' -State @{ state = 'CX_DEGRADED'; reason = 'smoke' } -Payload (New-BashPayload -Command 'node C:/tools/codex-companion.mjs cancel job-123')),

  (New-Case -Name 'allow_read_claude_md' -Expected 'allow' -Payload @{ tool_name = 'Read'; tool_input = @{ file_path = 'CLAUDE.md' } }),
  (New-Case -Name 'allow_read_agents_md' -Expected 'allow' -Payload @{ tool_name = 'Read'; tool_input = @{ file_path = 'AGENTS.md' } }),
  (New-Case -Name 'allow_read_project_md' -Expected 'allow' -Payload @{ tool_name = 'Read'; tool_input = @{ file_path = 'PROJECT.md' } }),
  (New-Case -Name 'allow_read_workflow_doc' -Expected 'allow' -Payload @{ tool_name = 'Read'; tool_input = @{ file_path = 'docs/workflows/cc-cx-delegation.md' } }),
  (New-Case -Name 'allow_grep_pattern_mentions_run' -Expected 'allow' -Payload @{ tool_name = 'Grep'; tool_input = @{ path = '.'; pattern = 'run/** and QuantProject/** are only search text' } }),
  (New-Case -Name 'deny_grep_path_run' -Expected 'deny' -RuleId 'PROTECTED_READ_DENIED' -Payload @{ tool_name = 'Grep'; tool_input = @{ path = 'run'; pattern = 'foo' } }),
  (New-Case -Name 'deny_json_fallback_prompt_path_no_raw_scan' -Expected 'deny' -RuleId 'JSON_PARSE_FAILED' -Raw '{"tool_name":"Read","tool_input":{"prompt":"please inspect run/** but this is malformed"'),

  (New-Case -Name 'deny_normal_read_run' -Expected 'deny' -RuleId 'PROTECTED_READ_DENIED' -Payload @{ tool_name = 'Read'; tool_input = @{ file_path = 'run/foo.py' } }),
  (New-Case -Name 'deny_normal_glob_run' -Expected 'deny' -RuleId 'PROTECTED_READ_DENIED' -Payload @{ tool_name = 'Glob'; tool_input = @{ pattern = 'run/**'; path = '.' } }),
  (New-Case -Name 'deny_normal_ls_quantproject' -Expected 'deny' -RuleId 'PROTECTED_READ_DENIED' -Payload @{ tool_name = 'LS'; tool_input = @{ path = 'QuantProject' } }),
  (New-Case -Name 'deny_normal_edit_run' -Expected 'deny' -RuleId 'PROTECTED_WRITE_DENIED' -Payload @{ tool_name = 'Edit'; tool_input = @{ file_path = 'run/foo.py' } }),
  (New-Case -Name 'deny_normal_write_run' -Expected 'deny' -RuleId 'PROTECTED_WRITE_DENIED' -Payload @{ tool_name = 'Write'; tool_input = @{ file_path = 'run/foo.py' } }),
  (New-Case -Name 'deny_normal_multiedit_run' -Expected 'deny' -RuleId 'PROTECTED_WRITE_DENIED' -Payload @{ tool_name = 'MultiEdit'; tool_input = @{ file_path = 'run/foo.py' } }),

  (New-Case -Name 'allow_bg_read_read_run' -Expected 'allow' -State @{ state = 'CC_BG_READ'; authorized_by = 'user'; reason = 'smoke' } -Payload @{ tool_name = 'Read'; tool_input = @{ file_path = 'run/foo.py' } }),
  (New-Case -Name 'allow_bg_read_grep_run' -Expected 'allow' -State @{ state = 'CC_BG_READ'; authorized_by = 'user'; reason = 'smoke' } -Payload @{ tool_name = 'Grep'; tool_input = @{ path = 'run'; pattern = 'foo' } }),
  (New-Case -Name 'allow_bg_read_glob_run' -Expected 'allow' -State @{ state = 'CC_BG_READ'; authorized_by = 'user'; reason = 'smoke' } -Payload @{ tool_name = 'Glob'; tool_input = @{ pattern = 'run/**'; path = '.' } }),
  (New-Case -Name 'allow_bg_read_ls_quantproject' -Expected 'allow' -State @{ state = 'CC_BG_READ'; authorized_by = 'user'; reason = 'smoke' } -Payload @{ tool_name = 'LS'; tool_input = @{ path = 'QuantProject' } }),
  (New-Case -Name 'deny_bg_read_bash_type_run' -Expected 'deny' -RuleId 'PROTECTED_BASH_READ_DENIED' -State @{ state = 'CC_BG_READ'; authorized_by = 'user'; reason = 'smoke' } -Payload (New-BashPayload -Command 'type run/foo.py')),

  (New-Case -Name 'allow_bg_write_edit_approved' -Expected 'allow' -State @{ state = 'CC_BG_WRITE'; approved_plan_id = 'plan-smoke'; approved_files = @('run/approved.py') } -Payload @{ tool_name = 'Edit'; tool_input = @{ file_path = 'run/approved.py' } }),
  (New-Case -Name 'allow_bg_write_write_approved' -Expected 'allow' -State @{ state = 'CC_BG_WRITE'; approved_plan_id = 'plan-smoke'; approved_files = @('run/approved.py') } -Payload @{ tool_name = 'Write'; tool_input = @{ file_path = 'run/approved.py' } }),
  (New-Case -Name 'allow_bg_write_multiedit_approved' -Expected 'allow' -State @{ state = 'CC_BG_WRITE'; approved_plan_id = 'plan-smoke'; approved_files = @('run/approved.py') } -Payload @{ tool_name = 'MultiEdit'; tool_input = @{ file_path = 'run/approved.py' } }),
  (New-Case -Name 'deny_bg_write_non_approved' -Expected 'deny' -RuleId 'CC_BG_WRITE_UNAPPROVED_FILE' -State @{ state = 'CC_BG_WRITE'; approved_plan_id = 'plan-smoke'; approved_files = @('run/approved.py') } -Payload @{ tool_name = 'Edit'; tool_input = @{ file_path = 'run/not-approved.py' } }),

  (New-Case -Name 'allow_codex_researcher_cmd_type_run' -Expected 'allow' -Payload (New-BashPayload -Command 'cmd /c type run/scraping/full_synergy_scraper.py' -Delegation $codexResearcherDelegation)),
  (New-Case -Name 'allow_codex_researcher_cmd_findstr_run' -Expected 'allow' -Payload (New-BashPayload -Command 'cmd /c findstr /n augment run/scraping/full_synergy_scraper.py' -Delegation $codexResearcherDelegation)),
  (New-Case -Name 'allow_codex_researcher_rg_run' -Expected 'allow' -Payload (New-BashPayload -Command 'rg -n "augment" run/scraping' -Delegation $codexResearcherDelegation)),
  (New-Case -Name 'allow_codex_researcher_git_ls_files_run' -Expected 'allow' -Payload (New-BashPayload -Command 'git ls-files run/data' -Delegation $codexResearcherDelegation)),
  (New-Case -Name 'deny_cc_direct_cmd_type_run' -Expected 'deny' -RuleId 'PROTECTED_BASH_READ_DENIED' -Payload (New-BashPayload -Command 'cmd /c type run/scraping/full_synergy_scraper.py')),
  (New-Case -Name 'deny_bash_execute_protected_script' -Expected 'deny' -RuleId 'PROTECTED_BASH_EXEC_DENIED' -Payload (New-BashPayload -Command 'python run/foo.py')),
  (New-Case -Name 'deny_bash_remove_protected' -Expected 'deny' -RuleId 'PROTECTED_BASH_WRITE_DENIED' -Payload (New-BashPayload -Command 'Remove-Item run/foo.py')),
  (New-Case -Name 'deny_bash_move_protected' -Expected 'deny' -RuleId 'PROTECTED_BASH_WRITE_DENIED' -Payload (New-BashPayload -Command 'Move-Item run/foo.py run/bar.py')),
  (New-Case -Name 'deny_bash_bulk_format_protected' -Expected 'deny' -RuleId 'PROTECTED_BASH_WRITE_DENIED' -Payload (New-BashPayload -Command 'prettier --write run')),
  (New-Case -Name 'deny_codex_researcher_redirect_run' -Expected 'deny' -RuleId 'CODEX_RESEARCHER_WRITE_DENIED' -Payload (New-BashPayload -Command 'echo x > run/foo.txt' -Delegation $codexResearcherDelegation)),

  (New-Case -Name 'allow_git_status' -Expected 'allow' -Payload (New-BashPayload -Command 'git status --short')),
  (New-Case -Name 'allow_git_diff' -Expected 'allow' -Payload (New-BashPayload -Command 'git diff --stat')),
  (New-Case -Name 'deny_git_add_unauthed' -Expected 'deny' -RuleId 'GIT_ADD_UNAUTHORIZED' -Payload (New-BashPayload -Command 'git add AGENTS.md')),
  (New-Case -Name 'deny_git_commit_unauthed' -Expected 'deny' -RuleId 'GIT_COMMIT_UNAUTHORIZED' -Payload (New-BashPayload -Command 'git commit -m "test"')),
  (New-Case -Name 'deny_git_push_unauthed' -Expected 'deny' -RuleId 'GIT_PUSH_REQUIRES_SEPARATE_AUTH' -Payload (New-BashPayload -Command 'git push origin HEAD')),
  (New-Case -Name 'deny_git_push_commit_auth_not_enough' -Expected 'deny' -RuleId 'GIT_PUSH_REQUIRES_SEPARATE_AUTH' -State @{ state = 'NORMAL'; git = @{ commit = $true; push = $false } } -Payload (New-BashPayload -Command 'git push origin HEAD')),
  (New-Case -Name 'allow_git_add_authed' -Expected 'allow' -State @{ state = 'NORMAL'; git = @{ add = $true } } -Payload (New-BashPayload -Command 'git add AGENTS.md')),
  (New-Case -Name 'allow_git_commit_authed' -Expected 'allow' -State @{ state = 'NORMAL'; git = @{ commit = $true } } -Payload (New-BashPayload -Command 'git commit -m "test"')),
  (New-Case -Name 'allow_git_push_separately_authed' -Expected 'allow' -State @{ state = 'NORMAL'; git = @{ push = $true } } -Payload (New-BashPayload -Command 'git push origin HEAD'))
)

$results = foreach ($case in $cases) {
  $stdoutFile = [System.IO.Path]::GetTempFileName()
  $stderrFile = [System.IO.Path]::GetTempFileName()
  $stateFile = [System.IO.Path]::GetTempFileName()
  Remove-Item -LiteralPath $stateFile -Force -ErrorAction SilentlyContinue
  $oldStatePath = $env:CC_CX_STATE_PATH
  $oldDebugMode = $env:CC_CX_GUARD_DEBUG_LOG
  $oldDebugPath = $env:CC_CX_GUARD_DEBUG_LOG_PATH

  try {
    if ($null -ne $case.State) {
      ConvertTo-Json -InputObject $case.State -Depth 8 -Compress | Set-Content -LiteralPath $stateFile -Encoding UTF8
    }

    $env:CC_CX_STATE_PATH = $stateFile
    $env:CC_CX_GUARD_DEBUG_LOG = 'off'
    $env:CC_CX_GUARD_DEBUG_LOG_PATH = ''

    $inputText = if (-not [string]::IsNullOrWhiteSpace([string]$case.Raw)) {
      [string]$case.Raw
    } else {
      ConvertTo-Json -InputObject $case.Payload -Depth 10 -Compress
    }

    $inputText | & pwsh -NoProfile -ExecutionPolicy Bypass -File $guardPath 1> $stdoutFile 2> $stderrFile
    $exitCode = $LASTEXITCODE
    $stdout = [string](Get-Content -Raw $stdoutFile -ErrorAction SilentlyContinue)
    $stderr = [string](Get-Content -Raw $stderrFile -ErrorAction SilentlyContinue)
  } finally {
    if ($null -eq $oldStatePath) { Remove-Item Env:CC_CX_STATE_PATH -ErrorAction SilentlyContinue } else { $env:CC_CX_STATE_PATH = $oldStatePath }
    if ($null -eq $oldDebugMode) { Remove-Item Env:CC_CX_GUARD_DEBUG_LOG -ErrorAction SilentlyContinue } else { $env:CC_CX_GUARD_DEBUG_LOG = $oldDebugMode }
    if ($null -eq $oldDebugPath) { Remove-Item Env:CC_CX_GUARD_DEBUG_LOG_PATH -ErrorAction SilentlyContinue } else { $env:CC_CX_GUARD_DEBUG_LOG_PATH = $oldDebugPath }
    Remove-Item -LiteralPath $stdoutFile, $stderrFile, $stateFile -Force -ErrorAction SilentlyContinue
  }

  $actual = 'allow'
  $reason = ([string]$stderr).Trim()
  $ruleId = ''

  if (-not [string]::IsNullOrWhiteSpace($stdout)) {
    try {
      $decision = $stdout | ConvertFrom-Json -ErrorAction Stop
      $actual = [string]$decision.hookSpecificOutput.permissionDecision
      $reason = [string]$decision.hookSpecificOutput.permissionDecisionReason
      $ruleId = [string]$decision.hookSpecificOutput.guardDecision.rule_id
    } catch {
      $actual = 'invalid-json'
      $reason = ([string]$stdout).Trim()
    }
  }

  $expectedRulePass = [string]::IsNullOrWhiteSpace([string]$case.RuleId) -or ([string]$case.RuleId -eq $ruleId)

  [pscustomobject]@{
    Name = $case.Name
    Expected = $case.Expected
    Actual = $actual
    RuleId = $ruleId
    Pass = (($case.Expected -eq $actual) -and $expectedRulePass)
    ExitCode = $exitCode
    Reason = $reason
  }
}

$results | Format-Table Name, Expected, Actual, RuleId, Pass -AutoSize

$failed = @($results | Where-Object { -not $_.Pass })
if ($failed.Count -gt 0) {
  ''
  'Failed cases:'
  $failed | Format-Table Name, Expected, Actual, RuleId, ExitCode, Reason -AutoSize
  exit 1
}

''
'All guard smoke cases passed.'
