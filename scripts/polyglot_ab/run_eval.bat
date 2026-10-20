@echo off
REM Robust wrapper: all logic lives in run_eval.py (so batch can't mangle it).
REM Usage:  run_eval.bat [model]     e.g.  run_eval.bat qwen3-coder:30b
cd /d "%~dp0"

set "PY="
where python >nul 2>&1 && set "PY=python"
if not defined PY ( where py >nul 2>&1 && set "PY=py -3" )
if not defined PY (
  echo [FATAL] Python 3 was not found on PATH. Install it, then re-run.
  echo         Tip: during install, tick "Add Python to PATH".
  pause
  exit /b 1
)

echo Using: %PY%
%PY% "%~dp0run_eval.py" %*
set "RC=%errorlevel%"
echo(
echo ============================================================
echo   run_eval finished with exit code %RC%
echo   (0 = all gates passed;  nonzero = a gate aborted above)
echo ============================================================
pause
exit /b %RC%
