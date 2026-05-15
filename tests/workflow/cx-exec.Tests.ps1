Set-StrictMode -Version Latest

$scriptPath = Join-Path $PSScriptRoot "..\..\scripts\workflow\cx-exec.ps1"
. $scriptPath

Describe "cx-exec.ps1" {
  BeforeEach {
    $script:TempRoot = Join-Path $env:TEMP ("cx-exec-test-" + [guid]::NewGuid().ToString("N"))
    New-Item -ItemType Directory -Path $script:TempRoot | Out-Null
    $script:OldApiKey = $env:CODEX_PROXY_API_KEY
  }

  AfterEach {
    if ($script:OldApiKey) {
      $env:CODEX_PROXY_API_KEY = $script:OldApiKey
    } else {
      Remove-Item Env:CODEX_PROXY_API_KEY -ErrorAction SilentlyContinue
    }
    Remove-Item -LiteralPath $script:TempRoot -Recurse -Force -ErrorAction SilentlyContinue
  }

  It "fails outside the active task worktree" {
    Mock Get-CxExecRepoRoot { $script:TempRoot }
    Mock Assert-CxExecActiveWorktree { throw "not active" }

    { Invoke-CxExec -CodexHome (Join-Path $script:TempRoot "home") -PromptFile "CODEX_PROMPT.md" -ResultFile "CODEX_RESULT.md" } |
      Should Throw "not active"
  }

  It "fails when CODEX_PROMPT.md is missing" {
    Mock Get-CxExecRepoRoot { $script:TempRoot }
    Mock Assert-CxExecActiveWorktree { }
    $env:CODEX_PROXY_API_KEY = "test-key"

    { Invoke-CxExec -CodexHome (Join-Path $script:TempRoot "home") -PromptFile "CODEX_PROMPT.md" -ResultFile "CODEX_RESULT.md" } |
      Should Throw "CODEX_PROMPT.md"
  }

  It "creates the CC config template without auth.json" {
    $codexHome = Join-Path $script:TempRoot "codex-home"

    $config = Ensure-CcCodexConfig -CodexHome $codexHome

    $config | Should Exist
    (Join-Path $codexHome "auth.json") | Should Not Exist
    $content = Get-Content -LiteralPath $config -Raw
    $content | Should Match 'model_provider = "codex-proxy"'
    $content | Should Match 'name = "Codex Proxy"'
    $content | Should Match 'base_url = "http://127\.0\.0\.1:8080/v1"'
    $content | Should Match 'env_key = "CODEX_PROXY_API_KEY"'
    $content | Should Match 'wire_api = "responses"'
  }

  It "passes the CC CODEX_HOME to the codex invocation" {
    $codexHome = Join-Path $script:TempRoot "codex-home"
    $prompt = Join-Path $script:TempRoot "CODEX_PROMPT.md"
    "respond OK" | Set-Content -LiteralPath $prompt -Encoding UTF8
    $env:CODEX_PROXY_API_KEY = "test-key"
    $script:SeenHome = $null

    Mock Get-CxExecRepoRoot { $script:TempRoot }
    Mock Assert-CxExecActiveWorktree { }
    Mock Invoke-CodexExec {
      param([string]$RepoRoot, [string]$CodexHome, [string]$PromptText)
      $script:SeenHome = $CodexHome
      return [pscustomobject]@{ ExitCode = 0; Stdout = "OK"; Stderr = "" }
    }

    Invoke-CxExec -CodexHome $codexHome -PromptFile $prompt -ResultFile (Join-Path $script:TempRoot "CODEX_RESULT.md")

    $script:SeenHome | Should Be $codexHome
  }

  It "fails fast when CODEX_PROXY_API_KEY is missing" {
    Remove-Item Env:CODEX_PROXY_API_KEY -ErrorAction SilentlyContinue

    { Assert-CodexProxyApiKey } | Should Throw "CODEX_PROXY_API_KEY is missing"
  }

  It "does not contain forbidden publishing or VS exec-home literals" {
    $scriptText = Get-Content -LiteralPath $scriptPath -Raw

    $scriptText | Should Not Match 'git commit'
    $scriptText | Should Not Match 'git push'
    $scriptText | Should Not Match 'gh pr'
    $scriptText | Should Not Match '\\.codex-exec\\'
  }
}
