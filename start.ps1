param([switch]$SeedDemo, [int]$Port = 8000, [int]$Workers = 2)
$ErrorActionPreference = 'Stop'
$projectRoot = $PSScriptRoot
$env:TEMP = 'D:\CodexWork\temp'
$env:TMP = $env:TEMP
$env:PIP_CACHE_DIR = 'D:\CodexWork\cache\pip'
$env:PYTHONDONTWRITEBYTECODE = '1'
New-Item -ItemType Directory -Force -Path $env:TEMP, $env:PIP_CACHE_DIR, "$projectRoot\artifacts" | Out-Null
if (-not $env:SENTINEL_ADMIN_TOKEN) {
    $adminFile = "$projectRoot\artifacts\admin-token.txt"
    if (-not (Test-Path -LiteralPath $adminFile)) {
        $randomBytes = [byte[]]::new(32)
        [System.Security.Cryptography.RandomNumberGenerator]::Fill($randomBytes)
        [Convert]::ToHexString($randomBytes) | Set-Content -LiteralPath $adminFile
    }
    $env:SENTINEL_ADMIN_TOKEN = (Get-Content -LiteralPath $adminFile -Raw).Trim()
}
$pythonPath = "$projectRoot\backend\.venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $pythonPath)) {
    python -m venv "$projectRoot\backend\.venv"
    if ($LASTEXITCODE -ne 0) { throw 'Failed to create Python environment' }
    & $pythonPath -m pip install -r "$projectRoot\backend\requirements.lock.txt"
    if ($LASTEXITCODE -ne 0) { throw 'Failed to install dependencies' }
}
if (-not $env:DATABASE_URL) { $env:DATABASE_URL = 'sqlite:///' + ($projectRoot.Replace('\','/') + '/backend/dev.db') }
$listener = Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue
if ($listener) { throw "Port $Port is already in use; select another port with -Port" }
$process = Start-Process -FilePath $pythonPath -ArgumentList @('-m','uvicorn','app.main:app','--host','127.0.0.1','--port',"$Port") -WorkingDirectory "$projectRoot\backend" -WindowStyle Hidden -PassThru -RedirectStandardOutput "$projectRoot\artifacts\server.out.log" -RedirectStandardError "$projectRoot\artifacts\server.err.log"
$ready = $false
for ($attempt = 0; $attempt -lt 50; $attempt++) {
    if ($process.HasExited) { throw "Server exited; inspect $projectRoot\artifacts\server.err.log" }
    try { $health = Invoke-RestMethod "http://127.0.0.1:$Port/health"; if ($health.status -eq 'ok') { $ready = $true; break } } catch { Start-Sleep -Milliseconds 200 }
}
if (-not $ready) { Stop-Process -Id $process.Id; throw 'Server startup timed out' }
Set-Content -LiteralPath "$projectRoot\artifacts\server.pid" -Value $process.Id
$workerPids = @()
for ($workerIndex = 0; $workerIndex -lt $Workers; $workerIndex++) {
    $workerProcess = Start-Process -FilePath $pythonPath -ArgumentList @('-m','app.worker') -WorkingDirectory "$projectRoot\backend" -WindowStyle Hidden -PassThru -RedirectStandardOutput "$projectRoot\artifacts\worker-$workerIndex.out.log" -RedirectStandardError "$projectRoot\artifacts\worker-$workerIndex.err.log"
    $workerPids += $workerProcess.Id
}
Set-Content -LiteralPath "$projectRoot\artifacts\workers.pid" -Value $workerPids
if ($SeedDemo) {
    & $pythonPath "$projectRoot\examples\release_demo.py" --host "http://127.0.0.1:$Port" --output "$projectRoot\artifacts\demo-report.json"
    if ($LASTEXITCODE -ne 0) { throw 'Demo seed failed; server remains running' }
}
Write-Output "SentinelLab: http://127.0.0.1:$Port/ (PID $($process.Id))"
