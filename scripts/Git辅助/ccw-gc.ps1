<#
中文简介：
- 这个文件是什么：清点并清理满足条件的 Git worktree 与关联分支。
- 什么时候读：需要做 worktree 垃圾回收，或先 dry-run 查看可清理项时。
- 约束什么：只处理 Claude Code managed root 下、干净且已合并到 base 的附属 worktree。
- 不负责什么：不强制清理 dirty 工作树，不跳过合并检查，也不删除主工作树。
#>

param(
  [string]$Base,

  [switch]$Apply,

  [string]$Root
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

# 解析当前 Git 仓库根目录，失败时立即终止。
function Resolve-RepoRoot {
  $root = git rev-parse --show-toplevel 2>$null
  if ($LASTEXITCODE -ne 0 -or -not $root) {
    throw "未在 Git 仓库内，无法解析 repo root。"
  }
  return (Resolve-Path $root).Path
}

# 推导默认 Claude Code managed root，限定清理范围。
function Get-DefaultCcRoot {
  $profile = [Environment]::GetFolderPath("UserProfile")
  if ([string]::IsNullOrWhiteSpace($profile)) {
    throw "无法解析用户目录，不能推导 Claude Code worktree root。"
  }
  return Join-Path (Join-Path $profile "_worktrees") "cc"
}

# 用完整路径边界判断归属，避免 C:\root 与 C:\root-old 这类前缀误判。
function Test-UnderRoot {
  param(
    [string]$Path,
    [string]$Root
  )
  $fullPath = [IO.Path]::GetFullPath($Path).TrimEnd('\','/')
  $fullRoot = [IO.Path]::GetFullPath($Root).TrimEnd('\','/')
  return $fullPath.Equals($fullRoot, [StringComparison]::OrdinalIgnoreCase) -or $fullPath.StartsWith("$fullRoot\", [StringComparison]::OrdinalIgnoreCase)
}

# 在未显式传入 -Base 时，按 origin/HEAD、当前分支、当前提交依次推断 base。
function Get-DefaultBase {
  param([string]$RepoRoot)
  $remoteHead = git -C $RepoRoot symbolic-ref refs/remotes/origin/HEAD 2>$null
  if ($LASTEXITCODE -eq 0 -and -not [string]::IsNullOrWhiteSpace($remoteHead)) {
    return $remoteHead.Trim()
  }
  $current = git -C $RepoRoot symbolic-ref -q HEAD 2>$null
  if ($LASTEXITCODE -eq 0 -and -not [string]::IsNullOrWhiteSpace($current)) {
    return ($current -replace '^refs/heads/','').Trim()
  }
  $hash = git -C $RepoRoot rev-parse --short HEAD 2>$null
  if ($LASTEXITCODE -eq 0 -and -not [string]::IsNullOrWhiteSpace($hash)) {
    return $hash.Trim()
  }
  throw "无法解析默认 base 分支，请通过 -Base 指定。"
}

# 解析 porcelain 输出，提取候选 worktree 元数据。
function Parse-WorktreeList {
  param([string]$RepoRoot)
  $raw = git -C $RepoRoot worktree list --porcelain
  if ($LASTEXITCODE -ne 0) { throw "无法读取 worktree 列表。" }
  $result = @()
  $entry = $null

  foreach ($line in $raw) {
    if ($line -match '^worktree\s+(?<path>.+)$') {
      if ($entry) { $result += $entry }
      $entry = @{ Path = $matches.path.Trim(); Branch = $null; Detached = $false; Protected = $false }
      continue
    }
    if (-not $entry) { continue }
    if ($line -match '^branch\s+(?<branch>.+)$') {
      $entry.Branch = $matches.branch.Trim()
    }
    if ($line -match '^detached$') {
      $entry.Detached = $true
    }
    if ($line -match '^(locked(?:\s|$)|bare$)') { $entry.Protected = $true }
  }
  if ($entry) { $result += $entry }
  return $result
}

# 确认工作树无未提交修改，避免误删在途工作。
function Test-Clean {
  param([string]$Path)
  $status = git -C $Path status --porcelain --untracked-files=all 2>$null
  if ($LASTEXITCODE -ne 0) { throw "无法确认工作树状态：$Path" }
  $items = @($status | Where-Object { $_ -match '\S' })
  return ($items.Count -eq 0)
}

# 只枚举文件名；用原始 stdout 保留 NUL 和合法文件名中的空白。
function Test-SensitiveIgnored {
  param([string]$Path)
  $process = [Diagnostics.Process]::new()
  $process.StartInfo.FileName = (Get-Command git -CommandType Application -ErrorAction Stop | Select-Object -First 1).Source
  $process.StartInfo.Arguments = 'ls-files --others --ignored --exclude-standard -z'
  $process.StartInfo.WorkingDirectory = $Path
  $process.StartInfo.UseShellExecute = $false
  $process.StartInfo.CreateNoWindow = $true
  $process.StartInfo.RedirectStandardOutput = $true
  $process.StartInfo.RedirectStandardError = $true
  $process.StartInfo.StandardOutputEncoding = [Text.UTF8Encoding]::new($false, $true)
  try {
    if (-not $process.Start()) { throw 'ignored-enumeration-failed' }
    $outputTask = $process.StandardOutput.ReadToEndAsync()
    $errorTask = $process.StandardError.ReadToEndAsync()
    $process.WaitForExit()
    $names = $outputTask.GetAwaiter().GetResult()
    $null = $errorTask.GetAwaiter().GetResult()
    if ($process.ExitCode -ne 0 -or ($names.Length -gt 0 -and -not $names.EndsWith([string][char]0))) {
      throw 'ignored-enumeration-failed'
    }
    if ($names.Length -eq 0) { return $false }
    foreach ($name in $names.Substring(0, $names.Length - 1).Split([char]0)) {
      # Git 对嵌套仓库可能只返回目录项；不得递归补查或当作已列出全部叶文件。
      if ($name.Length -eq 0 -or $name.EndsWith('/') -or $name.EndsWith('\')) {
        throw 'ignored-enumeration-incomplete'
      }
      try { $item = Get-Item -LiteralPath (Join-Path $Path $name) -Force -ErrorAction Stop }
      catch { throw 'ignored-enumeration-incomplete' }
      if ($item.PSIsContainer -or ($item.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
        throw 'ignored-enumeration-incomplete'
      }
      if ($name -match '(?i)(^|/)(\.env(?:\.[^/]*)?|auth\.json|local\.yaml|proxies\.json|accounts\.json|\.git-credentials|\.?(?:credentials?|secrets?|tokens?|cookies?|api[-_]?keys?|access[-_]?tokens?|refresh[-_]?tokens?|login[-_]?state|storage[-_]?state)(?:[._-][^/]*)?|Login Data|Local State)(/|$)' -or
          $name -match '(?i)(^|/)sms-monitor/(config\.json[^/]*|private-import\.txt[^/]*)$') {
        return $true
      }
    }
    return $false
  }
  finally { $process.Dispose() }
}

# 检查分支是否已被 base 包含，作为可清理前提。
function Get-MergeState {
  param([string]$RepoRoot,[string]$Branch,[string]$Base)
  git -C $RepoRoot merge-base --is-ancestor $Branch $Base | Out-Null
  if ($LASTEXITCODE -notin @(0, 1)) { throw "无法确认分支合并状态：$Branch" }
  return ($LASTEXITCODE -eq 0)
}

# 主流程：筛选候选项，默认 dry-run，只有 -Apply 时才真正删除。
try {
  $repoRoot = Resolve-RepoRoot
  $defaultRoot = Get-DefaultCcRoot
  if ([string]::IsNullOrWhiteSpace($Root)) {
    $Root = $defaultRoot
  }

  $baseRef = if ([string]::IsNullOrWhiteSpace($Base)) { Get-DefaultBase -RepoRoot $repoRoot } else { $Base.Trim() }

  $candidates = @()
  $skips = @()
  foreach ($wt in (Parse-WorktreeList -RepoRoot $repoRoot)) {
    if (-not (Test-UnderRoot -Path $wt.Path -Root $Root)) {
      $skips += [PSCustomObject]@{ Path = $wt.Path; Reason = "不在 Claude Code managed root 下" }
      continue
    }
    if ($wt.Path -ieq $repoRoot) {
      $skips += [PSCustomObject]@{ Path = $wt.Path; Reason = "主工作树" }
      continue
    }
    if ($wt.Protected) {
      $skips += [PSCustomObject]@{ Path = $wt.Path; Reason = "locked/bare 工作树" }
      continue
    }
    if ($wt.Detached -or -not $wt.Branch) {
      $skips += [PSCustomObject]@{ Path = $wt.Path; Reason = "detached HEAD" }
      continue
    }
    $branch = ($wt.Branch -replace '^refs/heads/','').Trim()
    if ([string]::IsNullOrWhiteSpace($branch)) {
      $skips += [PSCustomObject]@{ Path = $wt.Path; Reason = "分支不可解析" }
      continue
    }
    if ($branch -match '^(main|master|develop|dev|release/.*|hotfix/.*|prod/.*|codex/feat/xiangqi-local)$') {
      $skips += [PSCustomObject]@{ Path = $wt.Path; Reason = "protected branch"; Branch = $branch }
      continue
    }

    $branchExists = git -C $repoRoot show-ref --verify --quiet "refs/heads/$branch"
    if ($LASTEXITCODE -ne 0) {
      $skips += [PSCustomObject]@{ Path = $wt.Path; Reason = "关联分支不存在" ; Branch = $branch }
      continue
    }

    if (-not (Test-Clean -Path $wt.Path)) {
      $skips += [PSCustomObject]@{ Path = $wt.Path; Reason = "dirty"; Branch = $branch }
      continue
    }
    if (Test-SensitiveIgnored -Path $wt.Path) {
      $skips += [PSCustomObject]@{ Path = $wt.Path; Reason = "sensitive ignored path"; Branch = $branch }
      continue
    }

    if (-not (Get-MergeState -RepoRoot $repoRoot -Branch $branch -Base $baseRef)) {
      $skips += [PSCustomObject]@{ Path = $wt.Path; Reason = "未合并到 base=$baseRef"; Branch = $branch }
      continue
    }

    $candidates += [PSCustomObject]@{
      Path   = $wt.Path
      Branch = $branch
      Base   = $baseRef
    }
  }

  if ($skips.Count -gt 0) {
    Write-Host "跳过项："
    $skips | Format-Table Path,Branch,Reason -AutoSize
    Write-Host ""
  }

  if ($candidates.Count -eq 0) {
    Write-Host "无可清理项（dry-run）。"
    exit 0
  }

  Write-Host "可清理项："
  $candidates | Format-Table Path,Branch,Base -AutoSize
  Write-Host ""
  if (-not $Apply) {
    Write-Host "dry-run 模式：未执行删除。请使用 -Apply 真正清理。"
    exit 0
  }

  foreach ($item in $candidates) {
    $currentBranch = git -C $item.Path symbolic-ref -q HEAD
    if ($LASTEXITCODE -ne 0 -or $currentBranch -ne "refs/heads/$($item.Branch)" -or
        -not (Test-Clean -Path $item.Path) -or (Test-SensitiveIgnored -Path $item.Path) -or
        -not (Get-MergeState -RepoRoot $repoRoot -Branch $item.Branch -Base $baseRef)) {
      throw "清理前状态发生变化，保留工作树：$($item.Path)"
    }
    git -C $repoRoot worktree remove $item.Path
    if ($LASTEXITCODE -ne 0) {
      throw "工作树移除失败：$($item.Path)"
    }
    git -C $repoRoot branch -d $item.Branch
    if ($LASTEXITCODE -ne 0) {
      throw "分支删除失败：$($item.Branch)"
    }
  }
  Write-Host "已执行清理：$($candidates.Count) 个 worktree 与分支。"
  git -C $repoRoot worktree list
}
catch {
  Write-Error $_
  exit 1
}
