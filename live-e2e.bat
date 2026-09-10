@echo off
rem ---------------------------------------------------------------------------
rem  JARVIS - live end-to-end acceptance, with a person speaking
rem
rem  Brings up everything the run needs and then asks you to speak each
rem  scenario in turn. Double-click it, or run it from any command prompt.
rem
rem  Three things have to be running and none of them survive a reboot: the
rem  portable PostgreSQL, the backend, and the agent's connection to it. Doing
rem  them by hand means finding out about the second one after the first has
rem  already gone wrong, so they are done here in order.
rem
rem  The database and the backend are left running afterwards. Stop them with:
rem    .tools\pgsql\bin\pg_ctl.exe -D .pgdata stop
rem ---------------------------------------------------------------------------

setlocal
cd /d "%~dp0"

set "PY=%~dp0.venv\Scripts\python.exe"
if not exist "%PY%" (
    echo The project environment was not found. Rebuild it with: uv sync
    goto :failed
)

echo Starting PostgreSQL if it is not already up ...
".tools\pgsql\bin\pg_ctl.exe" -D .pgdata -o "-p 55432 -c listen_addresses=127.0.0.1" -l ".pgdata\server.log" start >nul 2>&1

rem pg_ctl returns before recovery finishes after an unclean shutdown, and the
rem backend's first query then fails with "the database system is starting up".
echo Waiting for it to accept connections ...
for /l %%i in (1,1,30) do (
    ".tools\pgsql\bin\pg_isready.exe" -h 127.0.0.1 -p 55432 >nul 2>&1
    if not errorlevel 1 goto :db_ready
    timeout /t 2 /nobreak >nul
)
echo PostgreSQL did not come up. See .pgdata\server.log
goto :failed

:db_ready
rem Checked before starting, not after: launching a second backend onto a port
rem the first already holds leaves a process failing silently in a minimised
rem window, while the health check passes and the run carries on regardless.
curl -s -o nul http://127.0.0.1:8000/v1/health && (
    echo The backend is already running.
    goto :backend_ready
)

echo Starting the backend ...
start "JARVIS backend" /min "%~dp0.venv\Scripts\atlas-backend.exe"

echo Waiting for it to answer ...
for /l %%i in (1,1,30) do (
    curl -s -o nul http://127.0.0.1:8000/v1/health && goto :backend_ready
    timeout /t 2 /nobreak >nul
)
echo The backend did not start.
goto :failed

:backend_ready
echo.
echo Ready. Speak each line when it is printed.
echo.
"%PY%" -u scripts\live_voice_e2e.py %*
if errorlevel 1 goto :failed
endlocal
exit /b 0

:failed
echo.
echo The run did not finish. The message above says why.
echo.
pause
endlocal
exit /b 1
