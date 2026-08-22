@echo off
rem ---------------------------------------------------------------------------
rem  JARVIS - Voice Enrollment
rem
rem  Double-click this, or run it from any command prompt. It needs no activated
rem  environment, no PATH entry and no knowledge of where anything lives.
rem
rem  It prefers the project's own virtual environment, which already contains a
rem  built atlas-agent.exe. That path needs neither uv nor Python on PATH, which
rem  matters here: uv was installed with `pip install --user`, so it sits in
rem  %%APPDATA%%\Python\Python314\Scripts while PATH only carries the Python
rem  installation's own Scripts directory under %%LOCALAPPDATA%%. Two different
rem  folders, which is exactly why `uv` is not found in a plain cmd.
rem
rem  uv is used only as a fallback, and only if the environment is missing.
rem ---------------------------------------------------------------------------

setlocal
cd /d "%~dp0"

set "AGENT=%~dp0.venv\Scripts\atlas-agent.exe"
if exist "%AGENT%" (
    "%AGENT%" enroll-voice %*
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
    echo   Expected one of:
    echo     %%APPDATA%%\Python\Python314\Scripts\uv.exe
    echo     %%LOCALAPPDATA%%\Programs\uv\uv.exe
    echo.
    echo   If the environment is simply missing, rebuild it with:
    echo     "%%APPDATA%%\Python\Python314\Scripts\uv.exe" sync
    echo.
    goto :failed
)

"%UV%" run atlas-agent enroll-voice %*

:done
if errorlevel 1 goto :failed
endlocal
exit /b 0

:failed
echo.
echo Voice enrollment did not start. The message above says why.
echo.
pause
endlocal
exit /b 1
