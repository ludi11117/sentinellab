$ErrorActionPreference = 'Stop'
$env:TEMP = 'D:\CodexWork\temp'
$env:TMP = $env:TEMP
$env:PYTHONDONTWRITEBYTECODE = '1'
New-Item -ItemType Directory -Force -Path $env:TEMP | Out-Null
& "$PSScriptRoot\backend\.venv\Scripts\python.exe" -m unittest discover -s "$PSScriptRoot\tests" -v
exit $LASTEXITCODE
