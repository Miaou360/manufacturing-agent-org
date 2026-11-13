param(
    [switch]$PrintXmlOnly = $false
)

$ErrorActionPreference = "Stop"
$jobDir = $PSScriptRoot
$collectBat = Join-Path $jobDir "run_d107_collect_0720.bat"
$checkBat = Join-Path $jobDir "run_d107_post0800_check.bat"
$conhost = "C:\Windows\System32\conhost.exe"
$currentUser = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name

function Build-TaskXml([string]$Desc, [string]$StartHm, [string]$BatPath) {
    return @"
<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.4" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>$Desc</Description>
  </RegistrationInfo>
  <Triggers>
    <CalendarTrigger>
      <StartBoundary>2026-10-30T$StartHm</StartBoundary>
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
    <ExecutionTimeLimit>PT15M</ExecutionTimeLimit>
    <Priority>7</Priority>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>$conhost</Command>
      <Arguments>--headless cmd.exe /c ""$BatPath""</Arguments>
      <WorkingDirectory>$jobDir</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"@
}

$xmlCollect = Build-TaskXml "01_JOB D107 (#182): Fresh post-07:00 ZTCODE_STOCK_REC+MB52+WMS collection at 07:18 AM for 07:20/07:30 delete candidate list." "07:18:00" $collectBat
$xmlCheck = Build-TaskXml "01_JOB D107 (#182): Post-08:00 cycle candidate status change verification at 08:08 AM." "08:08:00" $checkBat

if ($PrintXmlOnly) {
    Write-Output $xmlCollect
    Write-Output $xmlCheck
    exit 0
}

foreach ($pair in @(
    @{ Name = "SCM_Agent_Daily_GI_D107_Collect_0718"; Xml = $xmlCollect },
    @{ Name = "SCM_Agent_Daily_GI_D107_Check_0808";   Xml = $xmlCheck }
)) {
    $tmpXml = Join-Path $env:TEMP ($pair.Name + "_" + [guid]::NewGuid().ToString("N") + ".xml")
    try {
        [System.IO.File]::WriteAllText($tmpXml, $pair.Xml, [System.Text.Encoding]::Unicode)
        & schtasks.exe /create /tn $pair.Name /xml $tmpXml /f
        if ($LASTEXITCODE -ne 0) {
            throw "schtasks /create failed for $($pair.Name) with exit code $LASTEXITCODE"
        }
        Write-Host "✅ Registered Task: $($pair.Name)"
    } finally {
        Remove-Item -LiteralPath $tmpXml -Force -ErrorAction SilentlyContinue
    }
}

