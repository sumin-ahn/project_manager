@echo off
rem pm_config root facade (Windows) - thin forwarder.
rem
rem Resolves pm_config.py relative to this batch file location (%~dp0) and
rem forwards all arguments verbatim. No own arg parsing/validation -
rem pm_config is the single source of truth for the CLI contract.
rem (Callable as .\pm-config.cmd from both cmd and PowerShell.)
rem
rem Usage:  <manager>\pm-config.cmd repo add <name> --git <url> --test "<cmd>"
rem         <manager>\pm-config.cmd worktree add <repo>
rem         <manager>\pm-config.cmd status ^| whoami
rem         <manager>\pm-config.cmd release <slot> [--force]
rem         <manager>\pm-config.cmd update [--from <upstream>]
setlocal

rem Interpreter preference python -> py -> python3 (matches _detect_py Windows order).
rem Each candidate must run and satisfy Python 3.11+ (mirror of engine_rev.MIN_PYTHON).
set "PY="
where python >nul 2>nul && python --version >nul 2>nul && python "%~dp0.project_manager\tools\python_floor.py" >nul 2>nul && (set "PY=python" & goto :run)
where py >nul 2>nul && py --version >nul 2>nul && py "%~dp0.project_manager\tools\python_floor.py" >nul 2>nul && (set "PY=py" & goto :run)
where python3 >nul 2>nul && python3 --version >nul 2>nul && python3 "%~dp0.project_manager\tools\python_floor.py" >nul 2>nul && (set "PY=python3" & goto :run)

:run
if not defined PY set "PY=python"
rem Forward args verbatim + propagate rc.
"%PY%" "%~dp0.project_manager\tools\pm_config.py" %*
exit /b %ERRORLEVEL%
