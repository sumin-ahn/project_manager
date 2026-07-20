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
rem python <script> ignores shebang and is consistent, py is the launcher fallback, python3 is last resort.
set "PY=python"
where python >nul 2>nul && goto :run
where py >nul 2>nul && (set "PY=py" & goto :run)
where python3 >nul 2>nul && (set "PY=python3" & goto :run)

:run
rem Forward args verbatim + propagate rc.
"%PY%" "%~dp0.project_manager\tools\pm_config.py" %*
exit /b %ERRORLEVEL%
