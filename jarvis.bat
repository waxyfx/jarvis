@echo off
rem ---------------------------------------------------------------------------
rem  JARVIS - start listening
rem
rem  Double-click this, or run it from any command prompt. It connects the agent
rem  to the backend and opens the microphone: say "Jarvis" and it answers.
rem
rem  Same launcher logic as enroll-voice.bat, and for the same reason: uv was
rem  installed with `pip install --user` and sits in %%APPDATA%%\Python\...\Scripts,
rem  which is not on PATH in a plain cmd. The project's own virtual environment
rem  needs neither uv nor Python on PATH, so it is tried first.
rem
rem  Ctrl+C stops it. So does the tray icon, and so does the kill switch.
rem ---------------------------------------------------------------------------

setlocal
cd /d "%~dp0"

set "AGENT=%~dp0.venv\Scripts\atlas-agent.exe"
if exist "%AGENT%" (
    "%AGENT%" run --voice %*
    goto :done
)

echo The project environment was not found; falling back to uv.
echo.

rem Look for uv where it actually installs, not only on PATH.
set "UV="
for %%C in (
    "%APPDATA%\Python\Python314\Scripts\uv.exe"
    "%LOCALAPPDATA%\Programs\uv\uv.exe"
    "%USERPROFILE%\.local\bin\uv.exe"
    "%LOCALAPPDATA%\Microsoft\WinGet\Links\uv.exe"
) do if not defined UV if exist %%C set "UV=%%~C"

if not defined UV for %%X in (uv.exe) do if not defined UV set "UV=%%~$PATH:X"

if not defined UV (
    echo.
    echo Could not find uv, and there is no .venv in this folder.
    echo.
    echo   If the environment is simply missing, rebuild it with:
    echo     "%%APPDATA%%\Python\Python314\Scripts\uv.exe" sync
    echo.
    goto :failed
)

"%UV%" run atlas-agent run --voice %*

:done
if errorlevel 1 goto :failed
endlocal
exit /b 0

:failed
echo.
echo JARVIS did not start. The message above says why.
echo.
pause
endlocal
exit /b 1
