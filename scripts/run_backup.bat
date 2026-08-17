@echo off
setlocal
REM LifeOS scheduled backup launcher (called by Windows Task Scheduler)
REM Prepend Docker Desktop to PATH (scheduled-task env may not include it)
set "PATH=%PATH%;C:\Program Files\Docker\Docker\resources\bin"
REM Backup root override (matches lifeos_backup.py default)
set "LIFEOS_BACKUP_ROOT=E:\Backups\LifeOS"
"C:\Program Files\Python312\python.exe" "D:\WorkBuddy\LifeOS\scripts\lifeos_backup.py" >> "E:\Backups\LifeOS\backup.console.log" 2>&1
endlocal
