param(
    [Parameter(Mandatory=$true)][string]$Python,
    [Parameter(Mandatory=$true)][string]$LarkCli,
    [ValidateSet('DryRun','Live')][string]$Mode = 'DryRun',
    [Parameter(Mandatory=$true)][string]$OutputXml
)
$ErrorActionPreference = 'Stop'
# Export only: no registration, activation, credential prompt or business execution.
if ((Get-TimeZone).Id -ne 'China Standard Time') {
    throw 'Set this runner to China Standard Time before preparing its daily 10:00 task.'
}
$runnerRoot = Split-Path -Parent $PSScriptRoot
$pythonPath = (Resolve-Path -LiteralPath $Python).Path
$cliPath = (Resolve-Path -LiteralPath $LarkCli).Path
$scriptPath = Join-Path $runnerRoot 'tools\scheduled_runner.py'
$destination = [IO.Path]::GetFullPath($OutputXml)
if (Test-Path -LiteralPath $destination) { throw 'Refusing to overwrite an existing task export.' }
foreach ($value in @($pythonPath, $cliPath, $scriptPath)) {
    if ($value.Contains('"')) { throw 'Paths cannot contain quotation marks.' }
}
$flag = if ($Mode -eq 'Live') { '--execute' } else { '--dry-run' }
$arguments = '"' + $scriptPath + '" run ' + $flag + ' --cli "' + $cliPath + '"'
$action = New-ScheduledTaskAction -Execute $pythonPath -Argument $arguments -WorkingDirectory $runnerRoot
$trigger = New-ScheduledTaskTrigger -Daily -At '10:00'
$settings = New-ScheduledTaskSettingsSet -Disable -StartWhenAvailable -WakeToRun `
    -RunOnlyIfNetworkAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit ([TimeSpan]::Zero)
$account = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$principal = New-ScheduledTaskPrincipal -UserId $account -LogonType Password -RunLevel Limited
$task = New-ScheduledTask -Action $action -Trigger $trigger -Settings $settings -Principal $principal `
    -Description ('ebook daily 10:00; ' + $Mode + '; disabled until reviewed cutover')
$xml = $task | Export-ScheduledTask
$parent = Split-Path -Parent $destination
[IO.Directory]::CreateDirectory($parent) | Out-Null
[IO.File]::WriteAllText($destination, $xml, [Text.Encoding]::Unicode)
[pscustomobject]@{ Export = $destination; Mode = $Mode; Enabled = $false; Registered = $false; Account = $account }
