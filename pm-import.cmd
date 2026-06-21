@echo off
rem pm_import root facade (Windows) - thin forwarder.
rem
rem Resolves pm_import.py relative to this batch file location (%~dp0) and
rem forwards all arguments verbatim. No own arg parsing/validation -
rem pm_import is the single source of truth for the CLI contract.
rem (Callable as .\pm-import.cmd from both cmd and PowerShell.)
rem
rem Usage:  <manager>\pm-import.cmd --new <dest> --harness opencode
rem         (--from is auto-defaulted to the manager root by pm_import, so it can be omitted.)
setlocal

rem Interpreter preference python -> py -> python3 (matches _detect_py Windows order).
rem python <script> ignores shebang and is consistent, py is the launcher fallback, python3 is last resort.
set "PY=python"
where python >nul 2>nul && goto :run
where py >nul 2>nul && (set "PY=py" & goto :run)
where python3 >nul 2>nul && (set "PY=python3" & goto :run)

:run
rem Forward args verbatim + propagate rc.
"%PY%" "%~dp0.project_manager\tools\pm_import.py" %*
exit /b %ERRORLEVEL%
