# runtime_state 迁移脚本 V6.0 -> V6.1
# 将旧的单文件 runtime_state.json 按 endpoint 字段拆分为三个文件
# 在项目根目录执行：pwsh -File migrate_runtime_state.ps1

$oldFile = ".ai_workflow\runtime_state.json"

if (-not (Test-Path $oldFile)) {
    Write-Host "[SKIP] $oldFile 不存在，无需迁移" -ForegroundColor Yellow
    exit 0
}

$old = Get-Content $oldFile -Raw | ConvertFrom-Json

# 构建 V6.1 基础结构
$base = @{
    schema_version   = "6.1"
    workflow_id      = if ($old.workflow_id) { $old.workflow_id } else { "" }
    execution_status = if ($old.execution_status) { $old.execution_status } else { "idle" }
    audit_status     = if ($old.audit_status) { $old.audit_status } else { "idle" }
    merge_status     = if ($old.merge_status) { $old.merge_status } else { "blocked" }
    retry_count      = if ($old.retry_count) { $old.retry_count } else { 0 }
    is_new_project   = if ($null -ne $old.is_new_project) { $old.is_new_project } else { $false }
}

# 为每个端生成独立文件
foreach ($ep in @("cc", "ag", "cx")) {
    $target = ".ai_workflow\runtime_state_$ep.json"
    if (Test-Path $target) {
        Write-Host "[SKIP] $target 已存在，跳过（不覆盖）" -ForegroundColor Yellow
        continue
    }
    $state = $base.Clone()
    $state["endpoint"] = $ep
    # 非当前活跃端置为 idle
    if ($ep -ne "cc") {
        $state["execution_status"] = "idle"
        $state["audit_status"]     = "idle"
        $state["merge_status"]     = "blocked"
        $state["workflow_id"]      = ""
    }
    $state | ConvertTo-Json | Set-Content $target -Encoding UTF8
    Write-Host "[DONE] 已生成 $target" -ForegroundColor Green
}

# 备份旧文件（不删除，保留供参考）
$backup = ".ai_workflow\runtime_state.json.v60bak"
Copy-Item $oldFile $backup -Force
Write-Host "[BACKUP] 旧文件已备份至 $backup" -ForegroundColor Cyan
Write-Host "`n迁移完成。确认三个新文件内容正确后，可手动删除旧文件和备份。" -ForegroundColor Green
