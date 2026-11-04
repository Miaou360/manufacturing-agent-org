param(
    [switch]$PrintXmlOnly = $false
)

$ErrorActionPreference = "Stop"
$jobDir = $PSScriptRoot
$dailyBat = Join-Path $jobDir "run_mb51_daily_0215.bat"
$conhost = "C:\Windows\System32\conhost.exe"
$currentUser = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name

$taskName = "SCM_Agent_Daily_MB51_Collect_0215"
$desc = "06_JOB_Data_Collector (#191): Daily 02:15 EDT MB51 CPUDT catch-up (watermark+1 to closed KST yesterday, max 3d, SAP session lock, 02:50 hard cutoff, no email)."

$xml = @"
<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.4" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>$desc</Description>
  </RegistrationInfo>
  <Triggers>
    <CalendarTrigger>
      <StartBoundary>2026-10-30T02:15:00</StartBoundary>
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
    <ExecutionTimeLimit>PT40M</ExecutionTimeLimit>
    <Priority>7</Priority>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>$conhost</Command>
      <Arguments>--headless cmd.exe /c ""$dailyBat""</Arguments>
      <WorkingDirectory>$jobDir</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"@

if ($PrintXmlOnly) {
    Write-Output $xml
    exit 0
}

$tmpXml = Join-Path $env:TEMP ($taskName + "_" + [guid]::NewGuid().ToString("N") + ".xml")
try {
    [System.IO.File]::WriteAllText($tmpXml, $xml, [System.Text.Encoding]::Unicode)
    & schtasks.exe /create /tn $taskName /xml $tmpXml /f
    if ($LASTEXITCODE -ne 0) {
        throw "schtasks /create failed for $taskName with exit code $LASTEXITCODE"
    }
    Write-Host "✅ Registered Task: \$taskName"
} finally {
    Remove-Item -LiteralPath $tmpXml -Force -ErrorAction SilentlyContinue
}
