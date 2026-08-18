# LifeOS manual launcher (desktop shortcut target)
# already running -> open page directly; not running -> start in background, then open page
$portUp = Test-NetConnection -ComputerName 127.0.0.1 -Port 7208 -InformationLevel Quiet -WarningAction SilentlyContinue
if ($portUp) {
    Start-Process 'http://127.0.0.1:7208'
    exit 0
}
Start-Process powershell.exe -WindowStyle Hidden -ArgumentList '-NoProfile','-WindowStyle','Hidden','-ExecutionPolicy','Bypass','-File','D:\WorkBuddy\LifeOS\scripts\start_backend.ps1'
Add-Type -AssemblyName PresentationFramework
[System.Windows.MessageBox]::Show("LifeOS 后端正在后台启动，约 12 秒后自动打开页面。`n若未打开，请手动访问 http://127.0.0.1:7208", 'LifeOS 启动') | Out-Null
Start-Sleep -Seconds 12
if (Test-NetConnection -ComputerName 127.0.0.1 -Port 7208 -InformationLevel Quiet -WarningAction SilentlyContinue) {
    Start-Process 'http://127.0.0.1:7208'
} else {
    [System.Windows.MessageBox]::Show("启动似乎失败了，请查看日志 D:\WorkBuddy\LifeOS\data\autostart.log", 'LifeOS 启动') | Out-Null
}
