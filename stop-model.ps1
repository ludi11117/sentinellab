$ErrorActionPreference='Stop'
$pidPath=Join-Path $PSScriptRoot 'artifacts\model-server.pid'
if(-not(Test-Path -LiteralPath $pidPath)){Write-Output 'No recorded model process';exit 0}
$modelPid=[int](Get-Content -LiteralPath $pidPath)
$process=Get-CimInstance Win32_Process -Filter "ProcessId = $modelPid"
if(-not $process){Write-Output 'Model process has already exited';exit 0}
$manifest=Get-Content -LiteralPath "$PSScriptRoot\artifacts\model-running-manifest.json" -Raw | ConvertFrom-Json
if($process.ExecutablePath -ne $manifest.server_path -or $process.CommandLine -notmatch 'sentinel-qwen'){throw 'Recorded PID does not match our model server'}
Stop-Process -Id $modelPid
Write-Output 'Stopped recorded local model process'
