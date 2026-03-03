# Hextech System - Full Infrastructure Lock Script
$core = @(".ai_workflow", ".git\hooks", "scripts")

foreach ($c in $core) {
    if (Test-Path $c) {
        # Set to High Integrity
        icacls $c /setintegritylevel High
        # Deny Write/Delete for standard Users
        icacls $c /deny "BUILTIN\Users:(OI)(CI)(W,D)"
        # Grant Read-Only access
        icacls $c /grant "BUILTIN\Users:(OI)(CI)R"
        Write-Host " [SUCCESS] $c is LOCKED (Physical Lockdown Active)." -ForegroundColor Green
    } else {
        Write-Host " [SKIP] Path $c not found." -ForegroundColor Gray
    }
}