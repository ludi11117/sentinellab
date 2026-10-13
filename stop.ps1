$ErrorActionPreference = 'Stop'
$pidFile = Join-Path $PSScriptRoot 'artifacts\server.pid'
if (-not (Test-Path -LiteralPath $pidFile)) { Write-Output 'No recorded SentinelLab server'; exit 0 }
$serverPid = [int](Get-Content -LiteralPath $pidFile)
$serverProcess = Get-CimInstance Win32_Process -Filter "ProcessId = $serverPid"
if (-not $serverProcess) { Write-Output 'Recorded server has already exited'; exit 0 }
$expectedPython = Join-Path $PSScriptRoot 'backend\.venv\Scripts\python.exe'
if ($serverProcess.ExecutablePath -ne $expectedPython -or $serverProcess.CommandLine -notmatch 'uvicorn\s+app\.main:app') {
    throw 'Recorded PID does not match this project server; refusing to stop it'
}
function Stop-ProjectProcess([int]$targetPid, [string]$modulePattern) {
    # Windows venv python may be a launcher with a base-Python child process.
    $children = Get-CimInstance Win32_Process -Filter "ParentProcessId = $targetPid"
    foreach ($child in $children) {
        if ($child.CommandLine -like "*$expectedPython*" -and $child.CommandLine -match $modulePattern) {
            Stop-Process -Id $child.ProcessId -ErrorAction SilentlyContinue
        }
    }
    Stop-Process -Id $targetPid -ErrorAction SilentlyContinue
}
Stop-ProjectProcess $serverPid 'uvicorn\s+app\.main:app'
$workersFile = Join-Path $PSScriptRoot 'artifacts\workers.pid'
if (Test-Path -LiteralPath $workersFile) {
    foreach ($workerPid in Get-Content -LiteralPath $workersFile) {
        if (-not $workerPid) { continue }
        $workerProcess = Get-CimInstance Win32_Process -Filter "ProcessId = $([int]$workerPid)"
        if ($workerProcess -and $workerProcess.ExecutablePath -eq $expectedPython -and $workerProcess.CommandLine -match 'app\.worker') { Stop-ProjectProcess ([int]$workerPid) 'app\.worker' }
    }
}
Write-Output "Stopped SentinelLab PID $serverPid"
