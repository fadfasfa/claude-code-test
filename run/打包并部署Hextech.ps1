param(
    [switch]$RefreshData,
    [string]$VerifiedSnapshotRoot
)

# 这是显式发布入口：普通 `python -m tooling.build` 仍只生成 artifact。
$ErrorActionPreference = 'Stop'
$runRoot = $PSScriptRoot
$python = Join-Path $runRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
    throw "未找到打包 Python：$python"
}

$desktopRoot = if ($env:OneDrive) {
    Join-Path $env:OneDrive 'Desktop'
} else {
    [Environment]::GetFolderPath('Desktop')
}
$shortcut = Join-Path $desktopRoot 'Hextech伴生终端.lnk'
if (-not (Test-Path -LiteralPath $shortcut -PathType Leaf)) {
    throw "既有快捷方式不存在，拒绝创建重复入口：$shortcut"
}
$arguments = @(
    '-m', 'tooling.build',
    '--deploy',
    '--install-dir', 'C:\HextechCompanion',
    '--shortcut', $shortcut
)
if ($RefreshData) {
    $arguments += '--refresh-data'
}
if ($VerifiedSnapshotRoot) {
    $arguments += @('--verified-snapshot-root', $VerifiedSnapshotRoot)
}

Push-Location $runRoot
try {
    & $python @arguments
    if ($LASTEXITCODE -ne 0) {
        throw "Hextech 打包部署失败：exit=$LASTEXITCODE"
    }
} finally {
    Pop-Location
}
