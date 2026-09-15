@echo off
rem ---------------------------------------------------------------------------
rem  JARVIS - stop everything
rem
rem  Ctrl+C in the start-jarvis window already does this. This is for the other
rem  case: closing the window with the X button does not always let the launcher
rem  run its shutdown, and then the backend keeps running with no agent attached
rem  -- reminders still fire, and nothing is listening to deliver them.
rem
rem  Safe to run at any time. It stops only what JARVIS started.
rem ---------------------------------------------------------------------------

setlocal
cd /d "%~dp0"

powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "Get-Process -Name 'atlas-agent','atlas-backend' -ErrorAction SilentlyContinue | ForEach-Object { Write-Host ('  stopping ' + $_.Name); Stop-Process -Id $_.Id -Force }; & '%~dp0.tools\pgsql\bin\pg_ctl.exe' -D '%~dp0.pgdata' -m fast stop 2>&1 | Out-Null; Write-Host '  JARVIS stopped.'"

endlocal
