@echo off
rem ---------------------------------------------------------------------------
rem  JARVIS - start everything
rem
rem  Double-click this. It brings up the database, the backend and the agent,
rem  in that order, checking each one before starting the next, and then opens
rem  the microphone.
rem
rem  jarvis.bat starts only the agent, for when the backend is already running
rem  somewhere. This is the one to use on a machine that has just been switched
rem  on.
rem
rem  Ctrl+C stops the session and takes down whatever this started.
rem ---------------------------------------------------------------------------

setlocal
cd /d "%~dp0"

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\start_jarvis.ps1" %*
if errorlevel 1 goto :failed

endlocal
exit /b 0

:failed
echo.
echo JARVIS did not start. The message above says why.
echo.
pause
