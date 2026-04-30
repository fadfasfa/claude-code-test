<#
中文简介：阻止通过 Bash 执行危险 shell、git 和 worktree 命令。
何时读取：当 Claude Code 准备调用 Bash 工具时由 PreToolUse hook 读取。
约束内容：阻断原始 worktree 管理、危险 git 操作、全局配置写入和未受管下载或进程控制。
不负责：不调度任务、不自动继续、不修改业务文件，也不替代人工确认。
#>

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Write-Err([string]$Message) {
  [Console]::Error.WriteLine($Message)
}

# 统一阻断出口：报告原因并返回非零；不自动修复或转发执行。
function Block([string]$Reason) {
  Write-Err "worktree-governor: blocked unsafe shell command. $Reason Use approved repo-local tooling or ask explicitly."
  exit 2
}

# 从 Claude Code hook payload 的顶层或 tool_input 中提取字段。
function Get-JsonField($Object, [string[]]$Names) {
  if (-not $Object) { return $null }
  foreach ($name in $Names) {
    if ($Object.PSObject.Properties.Name -contains $name) { return $Object.$name }
  }
  return $null
}

# 识别 heredoc 结束标记，使命令切分时跳过正文内容。
function Get-HeredocTerminatorFromLine([string]$Line) {
  $inSingle = $false
  $inDouble = $false
  $escape = $false

  for ($i = 0; $i -lt $Line.Length - 1; $i++) {
    $ch = $Line[$i]

    if ($escape) {
      $escape = $false
      continue
    }

    if ($inSingle) {
      if ($ch -eq "'") { $inSingle = $false }
      continue
    }

    if ($inDouble) {
      if ($ch -eq '\') {
        $escape = $true
        continue
      }
      if ($ch -eq '"') { $inDouble = $false }
      continue
    }

    if ($ch -eq "'") {
      $inSingle = $true
      continue
    }

    if ($ch -eq '"') {
      $inDouble = $true
      continue
    }

    if ($ch -eq '<' -and $Line[$i + 1] -eq '<') {
      $j = $i + 2
      if ($j -lt $Line.Length -and $Line[$j] -eq '-') { $j++ }
      while ($j -lt $Line.Length -and [char]::IsWhiteSpace($Line[$j])) { $j++ }
      if ($j -ge $Line.Length) { return $null }

      $quote = $Line[$j]
      if ($quote -eq "'" -or $quote -eq '"') {
        $j++
        $start = $j
        while ($j -lt $Line.Length -and $Line[$j] -ne $quote) { $j++ }
        if ($j -gt $start) {
          return $Line.Substring($start, $j - $start)
        }
        return $null
      }

      $start = $j
      while ($j -lt $Line.Length -and -not [char]::IsWhiteSpace($Line[$j]) -and $Line[$j] -notin ';', '|', '&') { $j++ }
      if ($j -gt $start) {
        return $Line.Substring($start, $j - $start)
      }
      return $null
    }
  }

  return $null
}

# 移除 heredoc 正文，只保留外层命令结构用于安全匹配。
function Remove-HeredocBodies([string]$Text) {
  $lines = ($Text -replace "`r", "").Split("`n")
  $filtered = New-Object System.Collections.Generic.List[string]
  $pending = New-Object System.Collections.Generic.Queue[string]

  foreach ($line in $lines) {
    if ($pending.Count -gt 0) {
      if ($line.Trim() -eq $pending.Peek()) {
        $null = $pending.Dequeue()
      }
      continue
    }

    $filtered.Add($line)
    $terminator = Get-HeredocTerminatorFromLine $line
    if (-not [string]::IsNullOrWhiteSpace($terminator)) {
      $pending.Enqueue($terminator)
    }
  }

  return ($filtered -join "`n")
}

# 当前命令片段非空时加入结果集。
function Add-Segment([System.Collections.Generic.List[string]]$Segments, [System.Text.StringBuilder]$Current) {
  $text = $Current.ToString().Trim()
  if (-not [string]::IsNullOrWhiteSpace($text)) {
    $Segments.Add($text)
  }
  $Current.Clear() | Out-Null
}

# 按 shell 分隔符切分命令，同时尊重引号和 heredoc 边界。
function Get-CommandSegments([string]$Text) {
  $source = Remove-HeredocBodies $Text
  $segments = New-Object System.Collections.Generic.List[string]
  $current = New-Object System.Text.StringBuilder
  $inSingle = $false
  $inDouble = $false
  $escape = $false

  for ($i = 0; $i -lt $source.Length; $i++) {
    $ch = $source[$i]
    $next = if ($i + 1 -lt $source.Length) { $source[$i + 1] } else { [char]0 }

    if ($escape) {
      $null = $current.Append($ch)
      $escape = $false
      continue
    }

    if ($inSingle) {
      $null = $current.Append($ch)
      if ($ch -eq "'") { $inSingle = $false }
      continue
    }

    if ($inDouble) {
      $null = $current.Append($ch)
      if ($ch -eq '\') {
        $escape = $true
        continue
      }
      if ($ch -eq '"') { $inDouble = $false }
      continue
    }

    if ($ch -eq "'") {
      $null = $current.Append($ch)
      $inSingle = $true
      continue
    }

    if ($ch -eq '"') {
      $null = $current.Append($ch)
      $inDouble = $true
      continue
    }

    if ($ch -eq ';' -or $ch -eq "`n") {
      Add-Segment $segments $current
      continue
    }

    if ($ch -eq '|' -and $next -eq '|') {
      Add-Segment $segments $current
      $i++
      continue
    }

    if ($ch -eq '&' -and $next -eq '&') {
      Add-Segment $segments $current
      $i++
      continue
    }

    if ($ch -eq '|') {
      Add-Segment $segments $current
      continue
    }

    $null = $current.Append($ch)
  }

  Add-Segment $segments $current
  return $segments
}

# 匹配单个命令片段，并允许 FOO=bar 这类前置环境变量赋值。
function Test-SegmentMatch([string]$Segment, [string]$Pattern) {
  $prefix = '^\s*(?:(?:[A-Za-z_][A-Za-z0-9_]*)=(?:"[^"]*"|''[^'']*''|[^\s;|&]+)\s+)*'
  return $Segment -match ($prefix + $Pattern)
}

# 仅为封装脚本匹配规范化路径文本；不访问文件系统。
function Normalize-CommandPathText([string]$Text) {
  if ([string]::IsNullOrWhiteSpace($Text)) { return "" }
  $normalized = ($Text -replace '/', '\')
  return ($normalized -replace '\\+', '\').ToLowerInvariant()
}

# 只允许调用自带安全检查的仓库本地封装脚本。
function Test-AllowedWrapperSegment([string]$Segment) {
  $normalized = Normalize-CommandPathText $Segment
  if ($normalized -notmatch '^\s*(?:(?:[A-Za-z_][A-Za-z0-9_]*)=(?:"[^"]*"|''[^'']*''|[^\s;|&]+)\s+)*(?:powershell(?:\.exe)?|pwsh(?:\.exe)?)\b') {
    return $false
  }
  if ($normalized -notmatch '\s-file\s+') {
    return $false
  }

  $allowedWrappers = @(
    '\.claude\tools\worktree-governor\scan_agent_worktrees.ps1',
    '\.claude\tools\worktree-governor\scan_and_cleanup_agent_worktrees.ps1',
    '\.claude\tools\pr\ship_task_pr.ps1',
    '\.claude\tools\pr\review_local_pr.ps1'
  )

  foreach ($wrapper in $allowedWrappers) {
    if ($normalized.Contains($wrapper)) {
      return $true
    }
  }
  return $false
}

$stdin = [Console]::In.ReadToEnd()
if ([string]::IsNullOrWhiteSpace($stdin)) { exit 0 }

$command = ""
try {
  $event = $stdin | ConvertFrom-Json
  $command = [string](Get-JsonField $event @("command"))
  if ([string]::IsNullOrWhiteSpace($command) -and ($event.PSObject.Properties.Name -contains "tool_input")) {
    $command = [string](Get-JsonField $event.tool_input @("command"))
  }
}
catch {
  $command = $stdin
}

if ([string]::IsNullOrWhiteSpace($command)) { exit 0 }

$segments = Get-CommandSegments $command
foreach ($segment in $segments) {
  if (Test-AllowedWrapperSegment $segment) { continue }

  if (Test-SegmentMatch $segment '(?i:Remove-Item(?:\s|$))') { Block "Remove-Item is forbidden outside approved wrappers." }
  if (Test-SegmentMatch $segment '(?i:(?:rm|del|rmdir)\b.*\s(?:-Recurse|-r|-rf)\b)') { Block "recursive deletion is forbidden by default." }
  if (Test-SegmentMatch $segment '(?i:git\s+clean(?:\s|$))') { Block "git clean is forbidden by default." }
  if (Test-SegmentMatch $segment '(?i:git\s+reset\s+--hard(?:\s|$))') { Block "git reset --hard is forbidden by default." }
  if (Test-SegmentMatch $segment '(?i:git\s+branch\s+-(?:d|D)(?:\s|$))') { Block "raw branch deletion is forbidden; use approved cleanup tooling." }
  if (Test-SegmentMatch $segment '(?i:git\s+push(?:\s|$))') { Block "raw git push is forbidden; use /ship-task-pr." }
  if (Test-SegmentMatch $segment '(?i:(?:Invoke-WebRequest|iwr|irm|curl|wget)\b)') { Block "network download commands require explicit approval." }
  if (Test-SegmentMatch $segment '(?i:(?:Stop-Process|taskkill)\b)') { Block "process control requires explicit approval." }
  if (Test-SegmentMatch $segment '(?i:Set-ExecutionPolicy\b)') { Block "execution policy changes are forbidden by default." }
  if (Test-SegmentMatch $segment '(?i:npm\s+install\s+-g(?:\s|$))') { Block "global npm install is forbidden by default." }
  if (Test-SegmentMatch $segment '(?i:winget\s+install(?:\s|$))') { Block "winget install is forbidden by default." }

  $writesGlobalConfig = (Test-SegmentMatch $segment '(?i:(?:Set-Content|Add-Content|Out-File|New-Item|Remove-Item)\b)') -and ($segment -match '(?i)(?:C:\\Users\\apple\\\.(?:claude|codex)\b|C:/Users/apple/\.(?:claude|codex)\b|/c/Users/apple/\.(?:claude|codex)\b|/mnt/c/Users/apple/\.(?:claude|codex)\b)')
  if ($writesGlobalConfig) {
    Block "writes to global .claude or .codex are outside this repository."
  }

  if (Test-SegmentMatch $segment '(?i:git\s+worktree\s+add(?:\s|$))') { Block "git worktree add is governed." }
  if (Test-SegmentMatch $segment '(?i:git\s+worktree\s+move(?:\s|$))') { Block "git worktree move is not allowed for agents." }
  if (Test-SegmentMatch $segment '(?i:git\s+worktree\s+remove(?:\s|$))') { Block "raw worktree removal is forbidden; use approved cleanup tooling." }
}

exit 0
