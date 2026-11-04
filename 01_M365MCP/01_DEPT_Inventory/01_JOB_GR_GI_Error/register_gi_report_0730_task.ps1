param(
    [switch]$PrintXmlOnly = $false
)

$ErrorActionPreference = "Stop"
$jobDir = $PSScriptRoot
$batPath = Join-Path $jobDir "run_daily_gi_report_0730.bat"
$conhost = "C:\Windows\System32\conhost.exe"
$taskName = "SCM_Agent_Daily_GI_Report_0730"
$currentUser = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name

# D106 + 154-A XML Task Definition:
# - LogonType=InteractiveToken (required for Outlook COM session access)
# - DisallowStartIfOnBatteries=false & StopIfGoingOnBatteries=false
# - Local CalendarTriggers without UTC offset (DST-safe): 07:30 primary + 08:00 1-time retry
$xmlContent = @"
<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.4" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>01_JOB_GR_GI_Error (D106): Standalone DB-only previous-day GI resolution daily report at 07:30 AM local time, with 1 automatic retry check at 08:00 AM if 07:30 failed.</Description>
  </RegistrationInfo>
  <Triggers>
    <CalendarTrigger>
      <StartBoundary>2026-10-30T07:30:00</StartBoundary>
      <Enabled>true</Enabled>
      <ScheduleByDay>
        <DaysInterval>1</DaysInterval>
      </ScheduleByDay>
    </CalendarTrigger>
    <CalendarTrigger>
      <StartBoundary>2026-10-30T08:00:00</StartBoundary>
      <Enabled>true</Enabled>
      <ScheduleByDay>
        <DaysInterval>1</DaysInterval>
      </ScheduleByDay>
    </CalendarTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <UserId>$currentUser</UserId>
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <IdleSettings>
      <StopOnIdleEnd>false</StopOnIdleEnd>
      <RestartOnIdle>false</RestartOnIdle>
    </IdleSettings>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <Hidden>true</Hidden>
    <RunOnlyIfIdle>false</RunOnlyIfIdle>
    <DisallowStartOnRemoteAppSession>false</DisallowStartOnRemoteAppSession>
    <UseUnifiedSchedulingEngine>true</UseUnifiedSchedulingEngine>
    <WakeToRun>true</WakeToRun>
    <ExecutionTimeLimit>PT30M</ExecutionTimeLimit>
    <Priority>7</Priority>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>$conhost</Command>
      <Arguments>--headless cmd.exe /c ""$batPath""</Arguments>
      <WorkingDirectory>$jobDir</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"@

if ($PrintXmlOnly) {
    Write-Output $xmlContent
    exit 0
}

$tmpXml = Join-Path $env:TEMP ("SCM_Agent_Daily_GI_Report_0730_" + [guid]::NewGuid().ToString("N") + ".xml")
try {
    [System.IO.File]::WriteAllText($tmpXml, $xmlContent, [System.Text.Encoding]::Unicode)
    & schtasks.exe /create /tn $taskName /xml $tmpXml /f
    if ($LASTEXITCODE -ne 0) {
        throw "schtasks /create failed with exit code $LASTEXITCODE"
    }
    Write-Host "✅ Registered Task via 154-A XML: $taskName (Daily 07:30 primary + 08:00 retry | Battery Allowed | InteractiveToken)"
} finally {
    Remove-Item -LiteralPath $tmpXml -Force -ErrorAction SilentlyContinue
}
