Write-Host "Removing High Integrity physical lock..." -ForegroundColor Yellow

# Revert to Medium Integrity (Read & Write allowed)
icacls ".ai_workflow" /setintegritylevel "(OI)(CI)M"
icacls "scripts" /setintegritylevel "(OI)(CI)M"
icacls ".git\hooks" /setintegritylevel "(OI)(CI)M"

Write-Host "Core unlocked! Status: Read & Write allowed" -ForegroundColor Green