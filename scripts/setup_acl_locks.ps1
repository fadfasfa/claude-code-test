param([switch]$Unlock)

$ErrorActionPreference = 'Stop'
$targetDirs = @('.ai_workflow', 'scripts', '.git/hooks')

foreach ($dir in $targetDirs) {
    if (Test-Path $dir) {
        if ($Unlock) {
            Write-Host "[INFO] Reverting Integrity Level to Medium for: $dir"
            # 恢复为普通用户可写的中等完整性级别
            icacls $dir /setintegritylevel "(OI)(CI)M" /T /C /Q | Out-Null
        } else {
            Write-Host "[INFO] Setting High Integrity Level (MIC) for: $dir"
            # 设置为仅管理员可写的高完整性级别
            icacls $dir /setintegritylevel "(OI)(CI)H" /T /C /Q | Out-Null
        }
    } else {
        Write-Host "[WARN] Directory not found, skipping: $dir"
    }
}

if ($Unlock) { 
    Write-Host "[SUCCESS] Folders reverted to Standard User writable (Medium Integrity)." 
} else { 
    Write-Host "[SUCCESS] Folders locked to Administrator only (High Integrity)." 
}