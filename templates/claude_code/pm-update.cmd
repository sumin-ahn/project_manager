@echo off
rem pm_update root facade (Windows) - thin forwarder.
rem
rem Resolves pm_update.py relative to this batch file location (%~dp0) and
rem forwards all arguments verbatim. No own arg parsing/validation -
rem pm_update is the single source of truth for the CLI contract.
rem (Callable as .\pm-update.cmd from both cmd and PowerShell.)
rem
rem Usage:  cd <target> ^&^& .\pm-update.cmd
rem         (--from is auto-defaulted from local.conf upstream.path=, so it can be omitted.
rem          See .\pm-update.cmd --help for how to register it.)
setlocal

rem Interpreter preference python -> py -> python3 (matches _detect_py Windows order).
rem Each candidate must run the same-shebang probe and satisfy Python 3.11+.
set "PY="
where python >nul 2>nul && python --version >nul 2>nul && python "%~dp0.project_manager\tools\python_floor.py" >nul 2>nul && (set "PY=python" & goto :run)
where py >nul 2>nul && py --version >nul 2>nul && py "%~dp0.project_manager\tools\python_floor.py" >nul 2>nul && (set "PY=py" & goto :run)
where python3 >nul 2>nul && python3 --version >nul 2>nul && python3 "%~dp0.project_manager\tools\python_floor.py" >nul 2>nul && (set "PY=python3" & goto :run)

:run
if not defined PY set "PY=python"
rem Forward args verbatim + propagate rc.
"%PY%" "%~dp0.project_manager\tools\pm_update.py" %*
exit /b %ERRORLEVEL%
