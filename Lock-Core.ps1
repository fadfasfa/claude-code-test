Write-Host "Applying High Integrity physical lock..." -ForegroundColor Cyan

# Apply High Integrity (Read & Execute only for standard users)
icacls ".ai_workflow" /setintegritylevel "(OI)(CI)H"
icacls "scripts" /setintegritylevel "(OI)(CI)H"
icacls ".git\hooks" /setintegritylevel "(OI)(CI)H"

Write-Host "Core locked! Status: No-Write-Up (Read-Only mode active)" -ForegroundColor Green