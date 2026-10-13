param([int]$Port = 8081)
$ErrorActionPreference='Stop'
$projectRoot=$PSScriptRoot
$env:TEMP='D:\CodexWork\temp'
$env:TMP=$env:TEMP
$manifestPath='D:\CodexWork\cache\sentinellab-model\manifest.json'
if (-not (Test-Path -LiteralPath $manifestPath)) {
    & "$projectRoot\backend\.venv\Scripts\python.exe" "$projectRoot\tooling\setup_local_model.py"
    if ($LASTEXITCODE -ne 0) { throw 'Model setup failed' }
}
$manifest=Get-Content -LiteralPath $manifestPath -Raw | ConvertFrom-Json
if (Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue) { throw "Port $Port is already occupied" }
$keyFile=Join-Path $projectRoot 'artifacts\model-api-token.txt'
if(-not(Test-Path -LiteralPath $keyFile)){
    $bytes=[byte[]]::new(32)
    [System.Security.Cryptography.RandomNumberGenerator]::Fill($bytes)
    [Convert]::ToHexString($bytes)|Set-Content -LiteralPath $keyFile
}
$modelArgs=@('-m',$manifest.model_path,'--host','127.0.0.1','--port',"$Port",'--alias','sentinel-qwen','-c','4096','--parallel','1','--threads','6')
$modelArgs+=@('--api-key-file',$keyFile)
if($manifest.backend -eq 'vulkan'){$modelArgs+=@('--gpu-layers','99')}
$process=Start-Process -FilePath $manifest.server_path -ArgumentList $modelArgs -WorkingDirectory (Split-Path -Parent $manifest.server_path) -WindowStyle Hidden -PassThru -RedirectStandardOutput "$projectRoot\artifacts\model-server.out.log" -RedirectStandardError "$projectRoot\artifacts\model-server.err.log"
Set-Content -LiteralPath "$projectRoot\artifacts\model-server.pid" -Value $process.Id
Copy-Item -LiteralPath $manifestPath -Destination "$projectRoot\artifacts\model-running-manifest.json"
for($attempt=0;$attempt -lt 100;$attempt++) {
    if($process.HasExited){throw 'Model process exited; inspect artifacts/model-server.err.log'}
    try { $health=Invoke-RestMethod "http://127.0.0.1:$Port/health";if($health.status -eq 'ok'){Write-Output "Real local model ready: http://127.0.0.1:$Port/v1";exit 0} } catch { Start-Sleep -Milliseconds 300 }
}
throw 'Model startup timed out; process remains recorded in artifacts/model-server.pid'
