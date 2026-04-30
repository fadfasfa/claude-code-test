<#
中文简介：标记本仓原生 Read 阻断 hook 当前处于停用实验状态。
何时读取：排查 Read hook 配置或文本/代码文件读取失败边界时读取。
约束内容：文本/代码文件不得依赖带 pages 参数的原生 Read；失败后应改用 Grep/Glob/Bash 或 repo-explorer。
不负责：当前未注册执行，不提供替代读取结果，也不修改业务文件。
#>

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$stdin = [Console]::In.ReadToEnd()
# Claude Code hook payload 从 stdin 传入；空输入直接放行。
if (-not [string]::IsNullOrWhiteSpace($stdin)) {
  [Console]::Error.WriteLine("block-read-pages-for-text: disabled; no Read input normalization is performed.")
}

exit 0
