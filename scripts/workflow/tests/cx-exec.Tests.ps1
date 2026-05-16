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
    $text | Should Not Match 'Join-Path \$repoRoot "\.workflow"'
  }
}
