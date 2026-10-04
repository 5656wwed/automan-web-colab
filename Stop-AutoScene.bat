@echo off
REM Stop the local AutoScene Studio dashboard (whatever listens on port 7860).
powershell -NoProfile -Command "$p = Get-NetTCPConnection -LocalPort 7860 -State Listen -ErrorAction SilentlyContinue; if ($p) { $p | ForEach-Object { Write-Host ('Stopping AutoScene server (PID ' + $_.OwningProcess + ')...'); Stop-Process -Id $_.OwningProcess -Force } } else { Write-Host 'AutoScene is not running.' }; Write-Host 'Done.'"
"%SystemRoot%\System32\timeout.exe" /t 3 /nobreak >nul
