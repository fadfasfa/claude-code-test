# Hextech System - Full Infrastructure Unlock Script
$core = @(".ai_workflow", ".git\hooks", "scripts")

foreach ($c in $core) {
    if (Test-Path $c) {
        # Take ownership
        takeown /f $c /r /d y
        # Remove deny rules
        icacls $c /remove:d "BUILTIN\Users" /t /c /q
        # Set to Medium Integrity
        icacls $c /setintegritylevel Medium
        # Grant Full Access to current user
        icacls $c /grant "${env:USERNAME}:F" /t /q
        Write-Host " [SUCCESS] $c is UNLOCKED." -ForegroundColor Yellow
    } else {
        Write-Host " [SKIP] Path $c not found." -ForegroundColor Gray
    }
}