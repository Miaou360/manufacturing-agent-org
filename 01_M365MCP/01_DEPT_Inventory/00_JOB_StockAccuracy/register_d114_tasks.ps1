# Register D114 Shadow Incremental (Every 2 Hours at :12, between 01_JOB cycles)
# and Daily Morning Shadow Score Fallback (07:15 AM, right after 00_JOB Daily)
$ErrorActionPreference = "Stop"
$JobDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$IncBat = Join-Path $JobDir "run_d114_incremental.bat"
$DailyBat = Join-Path $JobDir "run_d114_daily_score.bat"

# 1. Every 2 hours at :12 (00:12, 02:12, ..., 22:12 — safely inside :06~:29 window)
$ActInc = New-ScheduledTaskAction -Execute "cmd.exe" -Argument "/c `"$IncBat`"" -WorkingDirectory $JobDir
$TrigInc = New-ScheduledTaskTrigger -Once -At "00:12" -RepetitionInterval (New-TimeSpan -Hours 2) -RepetitionDuration (New-TimeSpan -Days 3650)
$SetInc = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Minutes 20)
Register-ScheduledTask -TaskName "SCM_Agent_D114_Shadow_Incremental_2h" -Action $ActInc -Trigger $TrigInc -Settings $SetInc -Force | Out-Null

# 2. Daily at 07:15 AM (ensures D114_SHADOW_BIN_DAILY is recorded daily even if 00_JOB is skipped)
$ActDaily = New-ScheduledTaskAction -Execute "cmd.exe" -Argument "/c `"$DailyBat`"" -WorkingDirectory $JobDir
$TrigDaily = New-ScheduledTaskTrigger -Daily -At "07:15"
$SetDaily = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Minutes 30)
Register-ScheduledTask -TaskName "SCM_Agent_Daily_D114_Shadow_0715" -Action $ActDaily -Trigger $TrigDaily -Settings $SetDaily -Force | Out-Null

Write-Host "[OK] Registered SCM_Agent_D114_Shadow_Incremental_2h and SCM_Agent_Daily_D114_Shadow_0715"
