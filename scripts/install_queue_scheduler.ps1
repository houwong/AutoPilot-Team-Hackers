param(
    [string]$TaskName = "AutoPilot Ticket Queue Tick",
    [int]$IntervalMinutes = 5,
    [string]$RepoPath = (Get-Location).Path
)

$ErrorActionPreference = "Stop"
if ($IntervalMinutes -lt 1) { throw "IntervalMinutes must be at least 1." }
$tickScript = Join-Path $RepoPath "scripts\queue_tick.ps1"
if (-not (Test-Path -LiteralPath $tickScript)) { throw "Missing $tickScript" }

$action = New-ScheduledTaskAction `
    -Execute "PowerShell.exe" `
    -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$tickScript`""
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) `
    -RepetitionInterval (New-TimeSpan -Minutes $IntervalMinutes) `
    -RepetitionDuration (New-TimeSpan -Days 3650)

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Force | Out-Null
Write-Host "Installed '$TaskName' every $IntervalMinutes minute(s)."
Write-Host "The task requires QUEUE_TICK_TOKEN in the environment visible to PowerShell."
