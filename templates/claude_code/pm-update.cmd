@echo off
rem pm_update root facade (Windows) - thin forwarder.
rem
rem Resolves pm_update.py relative to this batch file location (%~dp0) and
rem forwards all arguments verbatim. No own arg parsing/validation -
rem pm_update is the single source of truth for the CLI contract.
rem (Callable as .\pm-update.cmd from both cmd and PowerShell.)
rem
rem Usage:  cd <target> ^&^& .\pm-update.cmd
rem         (--from is auto-defaulted from local.conf upstream=, so it can be omitted.
rem          See .\pm-update.cmd --help for how to register it.)
setlocal

rem Interpreter preference python -> py -> python3 (matches _detect_py Windows order).
rem python <script> ignores shebang and is consistent, py is the launcher fallback, python3 is last resort.
set "PY=python"
where python >nul 2>nul && goto :run
where py >nul 2>nul && (set "PY=py" & goto :run)
where python3 >nul 2>nul && (set "PY=python3" & goto :run)

:run
rem Forward args verbatim + propagate rc.
"%PY%" "%~dp0.project_manager\tools\pm_update.py" %*
exit /b %ERRORLEVEL%
