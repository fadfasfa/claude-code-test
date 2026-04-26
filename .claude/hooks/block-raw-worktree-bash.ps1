<#
中文简介：repo-local PreToolUse 安全拦截 hook。
什么时候读：需要理解本仓如何拦截危险 shell/git/worktree 命令时读取。
约束什么：拦截 recursive delete、git clean/reset、global config 写入、未经治理的 worktree 操作等高风险命令。
不负责什么：不调度任务，不自动继续执行，不修改业务文件，不替代人工确认。
#>

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Write-Err([string]$Message) {
  [Console]::Error.WriteLine($Message)
}

# 统一阻断出口：只报告原因并以非零码退出，不执行任何修复或调度。
function Block([string]$Reason) {
  Write-Err "worktree-governor: blocked unsafe shell command. $Reason Use approved repo-local tooling or ask explicitly."
  exit 2
}

# 从不同 Claude Code hook payload 形态中读取字段，兼容顶层和 tool_input。
function Get-JsonField($Object, [string[]]$Names) {
  if (-not $Object) { return $null }
  foreach ($name in $Names) {
    if ($Object.PSObject.Properties.Name -contains $name) { return $Object.$name }
  }
  return $null
}

# 识别 heredoc 终止符，后续命令切段会跳过 heredoc body，避免误拦截脚本正文。
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

# 移除 heredoc body，只保留外层命令结构；输入输出都是命令文本。
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

# 将当前切段加入 segments；空白段会被丢弃。
function Add-Segment([System.Collections.Generic.List[string]]$Segments, [System.Text.StringBuilder]$Current) {
  $text = $Current.ToString().Trim()
  if (-not [string]::IsNullOrWhiteSpace($text)) {
    $Segments.Add($text)
  }
  $Current.Clear() | Out-Null
}

# 按 shell 分隔符切分命令，同时尊重引号和 heredoc；输出用于逐段风险匹配。
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

# 匹配单个命令段，允许前缀环境变量赋值，例如 FOO=bar git clean。
function Test-SegmentMatch([string]$Segment, [string]$Pattern) {
  $prefix = '^\s*(?:(?:[A-Za-z_][A-Za-z0-9_]*)=(?:"[^"]*"|''[^'']*''|[^\s;|&]+)\s+)*'
  return $Segment -match ($prefix + $Pattern)
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
  if (Test-SegmentMatch $segment '(?i:(?:Remove-Item|rm|del|rmdir)\b.*\s(?:-Recurse|-r|-rf)\b)') { Block "recursive deletion is forbidden by default." }
  if (Test-SegmentMatch $segment '(?i:git\s+clean(?:\s|$))') { Block "git clean is forbidden by default." }
  if (Test-SegmentMatch $segment '(?i:git\s+reset\s+--hard(?:\s|$))') { Block "git reset --hard is forbidden by default." }
  if (Test-SegmentMatch $segment '(?i:git\s+branch\s+-D(?:\s|$))') { Block "forced branch deletion is forbidden." }
  if (Test-SegmentMatch $segment '(?i:(?:Invoke-WebRequest|iwr|irm|curl|wget)\b)') { Block "network download commands require explicit approval." }
  if (Test-SegmentMatch $segment '(?i:(?:Stop-Process|taskkill)\b)') { Block "process control requires explicit approval." }
  if (Test-SegmentMatch $segment '(?i:Set-ExecutionPolicy\b)') { Block "execution policy changes are forbidden by default." }
  if (Test-SegmentMatch $segment '(?i:npm\s+install\s+-g(?:\s|$))') { Block "global npm install is forbidden by default." }
  if (Test-SegmentMatch $segment '(?i:winget\s+install(?:\s|$))') { Block "winget install is forbidden by default." }

  $writesGlobalConfig = (Test-SegmentMatch $segment '(?i:(?:Set-Content|Add-Content|Out-File|New-Item|Remove-Item)\b)') -and ($segment -match '(?i)(?:C:\\Users\\apple\\\.(?:claude|codex)\b|/c/Users/apple/\.(?:claude|codex)\b|/mnt/c/Users/apple/\.(?:claude|codex)\b)')
  if ($writesGlobalConfig) {
    Block "writes to global .claude or .codex are outside this repository."
  }

  if (Test-SegmentMatch $segment '(?i:git\s+worktree\s+add(?:\s|$))') { Block "git worktree add is governed." }
  if (Test-SegmentMatch $segment '(?i:git\s+worktree\s+move(?:\s|$))') { Block "git worktree move is not allowed for agents." }
  if (Test-SegmentMatch $segment '(?i:git\s+worktree\s+remove\b.*\s(?:--force|-f)\b)') { Block "forced worktree removal is forbidden." }
  $removesDirectedWorktree = (Test-SegmentMatch $segment '(?i:git\s+worktree\s+remove(?:\s|$))') -and ($segment -match '(?i)(?:\bwt-directed-|_worktrees[\\/]+claudecode[\\/]+directed[\\/])')
  if ($removesDirectedWorktree) {
    Block "wt-directed-* worktrees are directed and require explicit cleanup instruction."
  }
}

exit 0
