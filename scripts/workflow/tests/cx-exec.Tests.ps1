<#
中文简介：
- 这个文件是什么：CC -> CX 入口的静态回归测试。
- 什么时候读：修改 cx-exec.ps1 或运行态路径后。
- 约束什么：根入口必须保持 delegator；真实 executor 必须使用 .state/workflow，不回退到 run/workflow 或 .workflow。
- 修改行为：只读测试，不写仓库状态。
#>

Set-StrictMode -Version Latest

$repoRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..\..\..")).Path
$rootEntry = Join-Path $repoRoot "cx-exec.ps1"
$workflowEntry = Join-Path $repoRoot "scripts\workflow\cx-exec.ps1"
$settingsEntry = Join-Path $repoRoot ".claude\settings.json"
$guardEntry = Join-Path $repoRoot ".claude\hooks\cc-delegation-guard.ps1"

function Invoke-DelegationGuardForTest {
  param(
    [Parameter(Mandatory = $true)]
    [object]$Payload,
    [hashtable]$Environment = @{}
  )

  $envNames = @("CLAUDE_PROJECT_DIR", "CC_CX_ALLOW_DIRECT_MODIFICATION")
  foreach ($key in $Environment.Keys) {
    if ($envNames -notcontains $key) {
      $envNames += $key
    }
  }

  $originalEnv = @{}
  foreach ($name in $envNames) {
    $originalEnv[$name] = [Environment]::GetEnvironmentVariable($name, "Process")
  }

  try {
    [Environment]::SetEnvironmentVariable("CLAUDE_PROJECT_DIR", $repoRoot, "Process")
    foreach ($key in $Environment.Keys) {
      [Environment]::SetEnvironmentVariable($key, [string]$Environment[$key], "Process")
    }

    $json = $Payload | ConvertTo-Json -Depth 8 -Compress
    $output = $json | powershell.exe -NoProfile -ExecutionPolicy Bypass -File $guardEntry
    if ([string]::IsNullOrWhiteSpace($output)) {
      return "allow"
    }
    return [string](($output | ConvertFrom-Json).hookSpecificOutput.permissionDecision)
  } finally {
    foreach ($name in $envNames) {
      [Environment]::SetEnvironmentVariable($name, $originalEnv[$name], "Process")
    }
  }
}

Describe "CC -> CX workflow entrypoints" {
  It "keeps the root entrypoint as a delegator" {
    $text = Get-Content -LiteralPath $rootEntry -Raw

    $text | Should Match 'scripts\\workflow\\cx-exec\.ps1'
    $text | Should Not Match '99-pipeline-smoke-test'
    $text | Should Not Match 'CODEX_RESULT\.md'
  }

  It "uses .state/workflow for runtime output" {
    $text = Get-Content -LiteralPath $workflowEntry -Raw

    $text | Should Match '\.state\\workflow'
    $text | Should Not Match 'Join-Path \$repoRoot "run\\workflow"'
    $text | Should Match 'codex-exec-wrapper\.exe'
    $text | Should Match 'C:\\Users\\apple\\.codex-exec'
    $text | Should Match 'Resolve-CxSandbox'
    $text | Should Not Match 'Join-Path \$repoRoot "\.workflow"'
  }
}

Describe "Claude Code project settings" {
  It "keeps low-friction read and repo-write permission baseline" {
    $settings = Get-Content -LiteralPath $settingsEntry -Raw | ConvertFrom-Json

    $settings.permissions.defaultMode | Should Be "acceptEdits"
    (@($settings.permissions.allow) -contains "Read") | Should Be $true
    (@($settings.permissions.allow) -contains "Edit(/**)") | Should Be $true
    (@($settings.permissions.allow) -contains "Write(/**)") | Should Be $true
    (@($settings.permissions.allow) -contains "MultiEdit(/**)") | Should Be $true
    (@($settings.permissions.ask) -contains "Bash") | Should Be $true
    (@($settings.permissions.deny) -contains "Read(//**/.env)") | Should Be $true
    (@($settings.permissions.deny) -contains "Read(//**/auth.json)") | Should Be $true

    [bool]$settings.sandbox.enabled | Should Be $true
    [bool]$settings.sandbox.failIfUnavailable | Should Be $false
    [bool]$settings.sandbox.autoAllowBashIfSandboxed | Should Be $true
    (@($settings.sandbox.filesystem.allowWrite) -contains ".") | Should Be $true
  }
}

Describe "CC delegation guard" {
  It "denies direct protected edits by default" {
    foreach ($toolName in @("Edit", "Write", "MultiEdit")) {
      Invoke-DelegationGuardForTest -Payload @{
        tool_name = $toolName
        tool_input = @{ file_path = "CLAUDE.md" }
      } | Should Be "deny"
    }
  }

  It "allows direct protected edits when Claude Code is in bypass permission mode" {
    Invoke-DelegationGuardForTest -Payload @{
      tool_name = "Edit"
      permission_mode = "bypassPermissions"
      tool_input = @{ file_path = "CLAUDE.md" }
    } | Should Be "allow"
  }

  It "allows direct protected edits when the CC-CX authorization environment flag is set" {
    Invoke-DelegationGuardForTest -Payload @{
      tool_name = "Write"
      tool_input = @{ file_path = "docs/workflows/10-cc-cx-orchestration.md" }
    } -Environment @{ CC_CX_ALLOW_DIRECT_MODIFICATION = "1" } | Should Be "allow"
  }

  It "allows standard cx-exec delegation command wrappers" {
    $commands = @(
      '.\cx-exec.ps1 -TaskId t -TaskDescription x -Profile review -Sandbox danger-full-access',
      'pwsh -NoProfile -File .\cx-exec.ps1 -TaskId t -TaskDescription x -Profile review -Sandbox danger-full-access',
      'pwsh -NoProfile -Command "& .\cx-exec.ps1 -TaskId t -TaskDescription x -Profile review -Sandbox danger-full-access"',
      'powershell.exe -NoProfile -Command "& .\cx-exec.ps1 -TaskId t -TaskDescription x -Profile review -Sandbox danger-full-access 2>&1"'
    )

    foreach ($command in $commands) {
      Invoke-DelegationGuardForTest -Payload @{
        tool_name = "Bash"
        tool_input = @{ command = $command }
      } | Should Be "allow"
    }
  }

  It "allows read-only and validation Bash against protected paths" {
    $commands = @(
      'Get-Content run\tmp.txt',
      'rg pattern run',
      'git diff -- run\tmp.txt',
      'python -m compileall run',
      'node --check run\app.js'
    )

    foreach ($command in $commands) {
      Invoke-DelegationGuardForTest -Payload @{
        tool_name = "Bash"
        tool_input = @{ command = $command }
      } | Should Be "allow"
    }
  }

  It "denies protected modifying Bash and destructive git by default" {
    $commands = @(
      'Set-Content run\tmp.txt x',
      'python -c "open(''run/tmp.txt'', ''w'').write(''x'')"',
      'node -e "require(''fs'').writeFileSync(''run/tmp.txt'', ''x'')"',
      'git restore -- run\tmp.txt',
      'git reset --hard',
      'git clean -fd'
    )

    foreach ($command in $commands) {
      Invoke-DelegationGuardForTest -Payload @{
        tool_name = "Bash"
        tool_input = @{ command = $command }
      } | Should Be "deny"
    }
  }
}
