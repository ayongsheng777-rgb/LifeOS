# LifeOS V2.0 backend autostart (registered as scheduled task "LifeOS-Backend", AtLogOn)
# 1. wait for docker data layer (postgres on 127.0.0.1:5433)
# 2. run uvicorn on host venv, log to data\autostart.log
$ErrorActionPreference = 'Continue'
Remove-Item Env:PYTHONPATH -ErrorAction SilentlyContinue
Set-Location 'D:\WorkBuddy\LifeOS'
$py  = 'D:\WorkBuddy\LifeOS\.venv\Scripts\python.exe'
$log = 'D:\WorkBuddy\LifeOS\data\autostart.log'

'[{0}] waiting for postgres 127.0.0.1:5433 ...' -f (Get-Date -Format 's') | Out-File $log -Append -Encoding utf8
$ready = $false
for ($i = 0; $i -lt 90; $i++) {
    if (Test-NetConnection -ComputerName 127.0.0.1 -Port 5433 -InformationLevel Quiet -WarningAction SilentlyContinue) {
        $ready = $true; break
    }
    Start-Sleep -Seconds 5
}
if (-not $ready) {
    '[{0}] postgres not ready after 7.5min, abort' -f (Get-Date -Format 's') | Out-File $log -Append -Encoding utf8
    exit 1
}
'[{0}] postgres ready, starting uvicorn on 127.0.0.1:7208' -f (Get-Date -Format 's') | Out-File $log -Append -Encoding utf8
& $py -m uvicorn app.main:app --host 127.0.0.1 --port 7208 2>&1 | Out-File $log -Append -Encoding utf8
