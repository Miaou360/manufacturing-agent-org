# ==============================================================================
# Windows Task Scheduler Setup for 01_DEPT_Inventory
# ==============================================================================
# Tasks Configured:
# 1. SCM_Agent_Daily_Stock_Accuracy: Daily 06:50 AM EST (Mon-Sat, Sunday Excluded per D38)
# 2. SCM_Agent_Daily_Backlog_0620: Daily 06:20 AM EST (Pre-refresh & Save, D48/D51)
# 3. SCM_Agent_Daily_Backlog_0815: Daily 08:15 AM EST (Morning Report & Resolution)
# 4. SCM_Agent_Continuous_GI_Error: At Logon (24/7 continuous: 08-17h: 30m, night: 1h)
# 5. SCM_Agent_Weekly_Backlog_Analysis: Weekly Friday 08:30 AM EST
# 6. SCM_Agent_Daily_System_Reconciliation: Daily 06:35 AM EST
# 7. SCM_Agent_Daily_SL01_R_Bins: Daily 04:15 AM EST (Mon-Sat, Sunday Excluded per D39)
# 8. SCM_Agent_Weekly_Hit_Rate: Weekly Monday 08:30 AM EST (User-only, D42/D43)
# ==============================================================================

$baseDir = $PSScriptRoot
$user = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name

Write-Host "Configuring Scheduled Tasks for User: $user"
Write-Host "Base Directory: $baseDir"

$conhost = "C:\Windows\System32\conhost.exe"

# Common Settings: Wake to run, allow on battery, start when available, Hidden window
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -WakeToRun -Hidden -ExecutionTimeLimit (New-TimeSpan -Hours 6)

# Helper: Ensure trigger uses Local System Time without fixed timezone offset (protects against DST shift)
function Get-LocalTrigger($trigger) {
    if ($trigger.StartBoundary -and $trigger.StartBoundary -match '^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})') {
        $trigger.StartBoundary = $matches[1]
    }
    return $trigger
}

# ------------------------------------------------------------------------------
# Task 1: SCM_Agent_Daily_Stock_Accuracy (Daily 365 Days 06:50 AM) - Headless Background
# ------------------------------------------------------------------------------
$saDir = Join-Path $baseDir "00_JOB_StockAccuracy"
$saBat = Join-Path $saDir "run_daily_pipeline.bat"
$saAction = New-ScheduledTaskAction -Execute $conhost -Argument "--headless `"$saBat`"" -WorkingDirectory $saDir
$saTrigger = Get-LocalTrigger (New-ScheduledTaskTrigger -Daily -At 6:50am)

try {
    Unregister-ScheduledTask -TaskName "SCM_Agent_Daily_Stock_Accuracy" -Confirm:$false -ErrorAction SilentlyContinue
} catch {}

Register-ScheduledTask -TaskName "SCM_Agent_Daily_Stock_Accuracy" -Action $saAction -Trigger $saTrigger -Settings $settings -Description "00_JOB_StockAccuracy: Daily WMS crawling & cycle count at 06:50 AM EST (D38). Headless background execution (Sunday collection active, email suppressed)."
Write-Host "✅ Registered Task: SCM_Agent_Daily_Stock_Accuracy (Daily 06:50 AM | Headless | 365-day Collection)"

# ------------------------------------------------------------------------------
# Task 2: SCM_Agent_Daily_Backlog_0620 (Daily 06:20 AM - Pre-Refresh) - Headless Background
# ------------------------------------------------------------------------------
$blDir = Join-Path $baseDir "02_JOB_Backlog_Clear"
$blBat = Join-Path $blDir "run_backlog_pipeline.bat"
$blAction06 = New-ScheduledTaskAction -Execute $conhost -Argument "--headless `"$blBat`" --run-now --pre-refresh" -WorkingDirectory $blDir
$blTrigger06 = Get-LocalTrigger (New-ScheduledTaskTrigger -Daily -At 6:20am)

try {
    Unregister-ScheduledTask -TaskName "SCM_Agent_Daily_Backlog_0600" -Confirm:$false -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName "SCM_Agent_Daily_Backlog_0610" -Confirm:$false -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName "SCM_Agent_Daily_Backlog_0620" -Confirm:$false -ErrorAction SilentlyContinue
} catch {}

Register-ScheduledTask -TaskName "SCM_Agent_Daily_Backlog_0620" -Action $blAction06 -Trigger $blTrigger06 -Settings $settings -Description "02_JOB_Backlog_Clear: Run 1 Pre-Refresh & Raw History Archive at 06:20 AM EST (D48/D51 - 06:00 GI settling). Headless background execution."
Write-Host "✅ Registered Task: SCM_Agent_Daily_Backlog_0620 (Daily 06:20 AM | Headless)"

# ------------------------------------------------------------------------------
# Task 3: SCM_Agent_Daily_Backlog_0815 (Daily 08:15 AM - Resolution & Report) - Headless Background
# ------------------------------------------------------------------------------
$blAction08 = New-ScheduledTaskAction -Execute $conhost -Argument "--headless `"$blBat`" --run-now --email" -WorkingDirectory $blDir
$blTrigger08 = Get-LocalTrigger (New-ScheduledTaskTrigger -Daily -At 8:15am)

try {
    Unregister-ScheduledTask -TaskName "SCM_Agent_Daily_Backlog_0800" -Confirm:$false -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName "SCM_Agent_Daily_Backlog_0815" -Confirm:$false -ErrorAction SilentlyContinue
} catch {}

Register-ScheduledTask -TaskName "SCM_Agent_Daily_Backlog_0815" -Action $blAction08 -Trigger $blTrigger08 -Settings $settings -Description "02_JOB_Backlog_Clear: Run 2 Final Morning Resolution & Cumulative Report at 08:15 AM EST. Headless background execution."
Write-Host "✅ Registered Task: SCM_Agent_Daily_Backlog_0815 (Daily 08:15 AM | Headless)"

# ------------------------------------------------------------------------------
# Task 4: SCM_Agent_Continuous_GI_Error (24/7 Daemon starting at Logon + 30m recovery) - Headless Background
# ------------------------------------------------------------------------------
$giDir = Join-Path $baseDir "01_JOB_GR_GI_Error"
$giBat = Join-Path $giDir "run_continuous_24h_pipeline.bat"
$giAction = New-ScheduledTaskAction -Execute $conhost -Argument "--headless `"$giBat`"" -WorkingDirectory $giDir
$giTriggerLogon = New-ScheduledTaskTrigger -AtLogOn -User $user
$giTriggerRepeat = Get-LocalTrigger (New-ScheduledTaskTrigger -Once -At "00:00" -RepetitionInterval (New-TimeSpan -Minutes 30))
$giSettings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -WakeToRun -Hidden -MultipleInstances IgnoreNew -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 5) -ExecutionTimeLimit (New-TimeSpan -Days 365)

try {
    Unregister-ScheduledTask -TaskName "SCM_Agent_Continuous_GI_Error" -Confirm:$false -ErrorAction SilentlyContinue
} catch {}

Register-ScheduledTask -TaskName "SCM_Agent_Continuous_GI_Error" -Action $giAction -Trigger @($giTriggerLogon, $giTriggerRepeat) -Settings $giSettings -Description "01_JOB_GR_GI_Error: 24/7 Continuous Autonomous Pipeline (Daytime 30m / Nighttime 1h). Headless background execution."
Write-Host "✅ Registered Task: SCM_Agent_Continuous_GI_Error (24/7 At Logon + 30m Auto-Recovery | Headless)"

# Task 4b (D106): SCM_Agent_Daily_GI_Report_0730 (Standalone DB-only Daily Report @ 07:30 + 08:00 retry via 154-A XML)
$giReportRegScript = Join-Path $giDir "register_gi_report_0730_task.ps1"
if (Test-Path $giReportRegScript) {
    & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $giReportRegScript
}

# ------------------------------------------------------------------------------
# Task 5: SCM_Agent_Weekly_Backlog_Analysis (Weekly Friday 08:30 AM EST) - Headless Background
# ------------------------------------------------------------------------------
$blaDir = Join-Path $baseDir "03_JOB_Backlog_Analysis"
$blaBat = Join-Path $blaDir "run_analysis.bat"
$blaAction = New-ScheduledTaskAction -Execute $conhost -Argument "--headless `"$blaBat`" --email" -WorkingDirectory $blaDir
$blaTrigger = Get-LocalTrigger (New-ScheduledTaskTrigger -Weekly -DaysOfWeek Friday -At 8:30am)

try {
    Unregister-ScheduledTask -TaskName "SCM_Agent_Weekly_Backlog_Analysis" -Confirm:$false -ErrorAction SilentlyContinue
} catch {}

Register-ScheduledTask -TaskName "SCM_Agent_Weekly_Backlog_Analysis" -Action $blaAction -Trigger $blaTrigger -Settings $settings -Description "03_JOB_Backlog_Analysis: Weekly COGI Time-Series Analytics & Deep Report at 08:30 AM EST (Runs after 08:00 GI and 08:15 Backlog complete). Headless background execution."
Write-Host "✅ Registered Task: SCM_Agent_Weekly_Backlog_Analysis (Weekly Friday 08:30 AM | Headless)"

# ------------------------------------------------------------------------------
# Task 6: SCM_Agent_Daily_System_Reconciliation (Daily 06:35 AM EST) - Headless Background
# ------------------------------------------------------------------------------
$recDir = Join-Path $baseDir "04_JOB_SystemReconciliation"
$recBat = Join-Path $recDir "run_systemreconciliation_pipeline.bat"
$recAction = New-ScheduledTaskAction -Execute $conhost -Argument "--headless `"$recBat`"" -WorkingDirectory $recDir
$recTrigger = Get-LocalTrigger (New-ScheduledTaskTrigger -Daily -At 6:35am)

try {
    Unregister-ScheduledTask -TaskName "SCM_Agent_Daily_System_Reconciliation" -Confirm:$false -ErrorAction SilentlyContinue
} catch {}

Register-ScheduledTask -TaskName "SCM_Agent_Daily_System_Reconciliation" -Action $recAction -Trigger $recTrigger -Settings $settings -Description "04_JOB_SystemReconciliation: Daily WMS vs SAP Official Inventory Accuracy extraction, DB loading & attribution report at 06:35 AM EST. Headless background execution."
Write-Host "✅ Registered Task: SCM_Agent_Daily_System_Reconciliation (Daily 06:35 AM | Headless)"

# ------------------------------------------------------------------------------
# Task 7: SCM_Agent_Daily_SL01_R_Bins (Daily 04:15 AM Mon-Sat) - Headless Background
# ------------------------------------------------------------------------------
$rDir = Join-Path $baseDir "05_JOB_SL01_R_Bins"
$rBat = Join-Path $rDir "run_sl01_r_bins_pipeline.bat"
$rAction = New-ScheduledTaskAction -Execute $conhost -Argument "--headless `"$rBat`"" -WorkingDirectory $rDir
$rTrigger = Get-LocalTrigger (New-ScheduledTaskTrigger -Daily -At 4:15am)

try {
    Unregister-ScheduledTask -TaskName "SCM_Agent_Daily_SL01_R_Bins" -Confirm:$false -ErrorAction SilentlyContinue
} catch {}

Register-ScheduledTask -TaskName "SCM_Agent_Daily_SL01_R_Bins" -Action $rAction -Trigger $rTrigger -Settings $settings -Description "05_JOB_SL01_R_Bins: Daily SL01 R-Bin lag monitoring & portrait Excel report at 04:15 AM EST. Headless background execution (Sunday excluded)."
Write-Host "✅ Registered Task: SCM_Agent_Daily_SL01_R_Bins (Daily 04:15 AM | Headless)"

# ------------------------------------------------------------------------------
# Task 8: SCM_Agent_Weekly_Hit_Rate (Weekly Monday 08:30 AM EST) - Headless Background
# ------------------------------------------------------------------------------
$hrDir = Join-Path $baseDir "00_JOB_StockAccuracy"
$hrBat = Join-Path $hrDir "run_weekly_hit_rate.bat"
$hrAction = New-ScheduledTaskAction -Execute $conhost -Argument "--headless `"$hrBat`"" -WorkingDirectory $hrDir
$hrTrigger = Get-LocalTrigger (New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday -At 8:30am)

try {
    Unregister-ScheduledTask -TaskName "SCM_Agent_Weekly_Hit_Rate" -Confirm:$false -ErrorAction SilentlyContinue
} catch {}

Register-ScheduledTask -TaskName "SCM_Agent_Weekly_Hit_Rate" -Action $hrAction -Trigger $hrTrigger -Settings $settings -Description "00_JOB_StockAccuracy: Weekly Floor Audit Hit Rate & Precision report at 08:30 AM EST (D42/D43). Headless background execution (User-only recipient)."
Write-Host "✅ Registered Task: SCM_Agent_Weekly_Hit_Rate (Weekly Monday 08:30 AM | Headless)"


Write-Host "`nAll 01_DEPT_Inventory tasks successfully configured!"


