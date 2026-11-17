@echo off
rem Launch the document camera server, preferring the project virtualenv.
rem Extra arguments are passed through: run.cmd --desktop-fit
setlocal
set "HERE=%~dp0"
set "PY=%HERE%.venv\Scripts\python.exe"

if exist "%PY%" goto run

where python >nul 2>nul
if errorlevel 1 (
  echo Python was not found on PATH.
  echo Create the environment first:  python -m venv .venv
  echo Then install:                  .venv\Scripts\python -m pip install -r requirements.txt
  exit /b 1
)
set "PY=python"
echo [run.cmd] .venv not found, falling back to the Python on PATH.

:run
"%PY%" "%HERE%server.py" %*
exit /b %ERRORLEVEL%
